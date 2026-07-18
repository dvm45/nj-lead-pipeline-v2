"""
Ingest orchestrator — the engine behind `njlead ingest <folder>`.

This module does five things for each PDF it finds:
  1. Compute a SHA-256 fingerprint → skip the file if already ingested
  2. Call extractor.py to pull raw text out of each page
  3. Store the raw text in the `pages` table (so you can inspect it later)
  4. Call parser.py on each page to find school names, samples, and measurements
  5. Write the results to the database; log anything that goes wrong to `issues`

At the end, it prints a summary:
  Ingested: 12  |  Skipped (duplicate): 3  |  Failed: 1

Design note: nothing in this file raises an exception to the caller.
All errors are caught, logged to the issues table, and counted.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from njlead.db.models import Document, Issue, Measurement, Page, Sample, School
from njlead.db.session import get_session
from njlead.ingest.extractor import extract_pdf
from njlead.ingest.parser import ParsedPage, parse_page


# ---------------------------------------------------------------------------
# SHA-256 fingerprint
# ---------------------------------------------------------------------------

def _hash_file(path: Path) -> str:
    """
    Compute a SHA-256 hash of a file's contents.

    Used to detect duplicate files — if the same PDF is in two folders
    with different names, we only ingest it once.
    """
    h = hashlib.sha256()
    # Read in 64KB chunks to avoid loading huge PDFs into memory all at once
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# School deduplication
# ---------------------------------------------------------------------------

def _get_or_create_school(
    session: Session, name: str | None, district: str | None
) -> School | None:
    """
    Look up a school by name + district, creating a new row if needed.

    This prevents duplicate school rows when the same school appears
    in many different PDF reports.

    Returns None if both name and district are None.
    """
    if not name and not district:
        return None

    # The schools table requires a name — if we only have a district,
    # skip creating the school row rather than crashing
    if not name:
        return None

    # Normalize: strip whitespace, title-case
    name = name.strip().title() if name else None
    district = district.strip().title() if district else None

    # Check if this school already exists
    existing = (
        session.query(School)
        .filter(School.name == name, School.district == district)
        .first()
    )
    if existing:
        return existing

    # First time we've seen this school — create it
    school = School(name=name, district=district, state="NJ")
    session.add(school)
    session.flush()  # assigns school.id without committing the full transaction
    return school


# ---------------------------------------------------------------------------
# Write parsed page data to the database
# ---------------------------------------------------------------------------

def _write_parsed_data(
    session: Session,
    doc: Document,
    page_num: int,
    parsed: ParsedPage,
) -> int:
    """
    Write a parsed page's measurements to the database.

    Returns the number of measurements written.
    """
    if not parsed.measurements:
        return 0

    # Get or create the school row
    school = _get_or_create_school(session, parsed.school_name, parsed.district)

    measurements_written = 0
    for m in parsed.measurements:
        # Create a sample row for each measurement
        sample = Sample(
            document_id=doc.id,
            school_id=school.id if school else None,
            sample_id=m.sample_id,
            location=m.location,
            fixture_type=m.fixture_type,
            sample_date=m.sample_date,
            test_year=m.test_year,
        )
        session.add(sample)
        session.flush()  # assigns sample.id

        # Compute whether this result exceeds the action level
        exceeds = None
        if m.action_level_ppb is not None:
            exceeds = m.result_ppb > m.action_level_ppb

        measurement = Measurement(
            sample_id=sample.id,
            analyte="lead",
            result_ppb=m.result_ppb,
            action_level_ppb=m.action_level_ppb,
            exceeds_action_level=exceeds,
        )
        session.add(measurement)
        measurements_written += 1

    return measurements_written


# ---------------------------------------------------------------------------
# Issue logging helpers
# ---------------------------------------------------------------------------

def _log_issue(
    session: Session,
    doc: Document,
    issue_type: str,
    detail: str,
    page_num: int | None = None,
) -> None:
    """Write one row to the issues table."""
    issue = Issue(
        document_id=doc.id,
        page_num=page_num,
        issue_type=issue_type,
        detail=detail,
        created_at=datetime.now(timezone.utc),
    )
    session.add(issue)


# ---------------------------------------------------------------------------
# Single-file ingest
# ---------------------------------------------------------------------------

def _ingest_one(session: Session, path: Path, engine: str = "regex") -> str:
    """
    Ingest one PDF file.

    engine:
      "regex" (default) — original multi-strategy parser, page by page.
      "llm"             — one Claude API call for the whole report; the schema
                          and validation gate live in njlead/ingest/*.

    Returns one of: 'ingested', 'skipped', 'failed'
    """
    now = datetime.now(timezone.utc)

    # --- Step 1: Deduplication ---
    file_hash = _hash_file(path)
    existing = session.query(Document).filter(Document.file_hash == file_hash).first()
    if existing:
        return "skipped"

    # --- Step 2: Create the document row ---
    doc = Document(
        file_path=str(path.resolve()),
        file_name=path.name,
        file_hash=file_hash,
        ingested_at=now,
        status="ok",  # optimistic — will downgrade if problems arise
    )
    session.add(doc)
    session.flush()  # assigns doc.id

    # --- Step 3: Extract text from PDF ---
    extracted = extract_pdf(path)
    doc.page_count = extracted.page_count

    if extracted.failed:
        # Couldn't open the file at all
        doc.status = "failed"
        _log_issue(session, doc, "extraction_failed", extracted.error)
        session.commit()
        return "failed"

    # --- Step 4: Store raw page text ---
    # Both engines share this step so the raw text is always browsable with
    # /show-sample, regardless of which extractor filled the samples table.
    for extracted_page in extracted.pages:
        page_row = Page(
            document_id=doc.id,
            page_num=extracted_page.page_num,
            raw_text=extracted_page.raw_text,
            is_blank=extracted_page.is_blank,
        )
        session.add(page_row)

    # --- Step 5: Parse (engine-specific) ---
    if engine == "llm":
        total_measurements, has_issues = _parse_with_llm(session, doc, extracted, path)
    else:
        total_measurements, has_issues = _parse_with_regex(session, doc, extracted)

    # If we processed pages but found zero measurements across the whole file
    if total_measurements == 0 and not extracted.failed:
        _log_issue(
            session, doc, "no_measurements",
            "No lead measurement values were extracted from any page in this file.",
        )
        has_issues = True

    # Downgrade status if there were any issues
    if has_issues and total_measurements > 0:
        doc.status = "partial"
    elif has_issues and total_measurements == 0:
        doc.status = "failed"

    session.commit()
    return "ingested"


# ---------------------------------------------------------------------------
# Regex engine — the original page-by-page loop, now factored out so the LLM
# branch can sit next to it without one path stepping on the other.
# ---------------------------------------------------------------------------

def _parse_with_regex(session: Session, doc: Document, extracted) -> tuple[int, bool]:
    """
    Run the regex parser strategies over each extracted page.

    Returns (total_measurements, has_issues).
    """
    total_measurements = 0
    has_issues = False

    for extracted_page in extracted.pages:
        if extracted_page.is_blank:
            # Scanned image page — no text to parse, flag for OCR
            _log_issue(
                session, doc, "ocr_needed",
                "Page returned no text — likely a scanned image. "
                "OCR support can be added later.",
                page_num=extracted_page.page_num,
            )
            has_issues = True
            continue

        try:
            parsed = parse_page(extracted_page.raw_text)
        except Exception as e:
            _log_issue(
                session, doc, "parse_error",
                f"Unexpected error during parsing: {e}",
                page_num=extracted_page.page_num,
            )
            has_issues = True
            continue

        # Log if we couldn't find a school name (only matters on first page)
        if extracted_page.page_num == 0 and parsed.school_name is None:
            _log_issue(
                session, doc, "no_school_found",
                "Could not find school name on first page.",
                page_num=0,
            )
            has_issues = True

        n = _write_parsed_data(session, doc, extracted_page.page_num, parsed)
        total_measurements += n

    return total_measurements, has_issues


# ---------------------------------------------------------------------------
# LLM engine — one API call per report through llm_pipeline.
# ---------------------------------------------------------------------------

def _parse_with_llm(
    session: Session, doc: Document, extracted, pdf_path: Path
) -> tuple[int, bool]:
    """
    Send the full report to the LLM engine.

    Returns (total_measurements, has_issues).

    We build a simple list of page dicts (page_num / raw_text / is_blank) so
    the LLM pipeline doesn't need to import our internal ExtractedPage type.
    """
    # Lazy import: keeps the anthropic SDK optional at import time and avoids
    # an accidental circular import (llm_pipeline imports from this module).
    from njlead.ingest.llm_pipeline import ingest_report_llm

    pages_payload = [
        {
            "page_num": p.page_num,
            "raw_text": p.raw_text,
            "is_blank": p.is_blank,
        }
        for p in extracted.pages
    ]

    # LLMConfigError (missing key / SDK) is deliberately NOT caught here — it
    # would repeat on every file, so the loader propagates it and the caller
    # aborts the whole run with setup instructions.
    return ingest_report_llm(session, doc, pages_payload, pdf_path)


# ---------------------------------------------------------------------------
# Folder walk — the public entry point
# ---------------------------------------------------------------------------

def ingest_folder(
    folder: Path,
    engine: str = "regex",
    limit: int | None = None,
) -> dict[str, int]:
    """
    Walk a folder recursively and ingest every PDF file found.

    engine: which extractor to use — "regex" (default) or "llm".
    limit:  process at most this many files. Handy for a cheap first LLM run
            (e.g. --engine llm --limit 3) before committing to the whole set.

    Returns a summary dict:
      { 'ingested': N, 'skipped': N, 'failed': N, 'total': N }

    Errors on individual files are caught and counted — the loop always
    continues to the next file. The one exception is LLMConfigError (missing
    API key or SDK): that repeats on every file, so we let it propagate and
    the caller aborts the run with setup instructions.
    """
    counts = {"ingested": 0, "skipped": 0, "failed": 0}

    # Find all PDF files (case-insensitive on Windows)
    pdf_files = sorted(folder.rglob("*.pdf")) + sorted(folder.rglob("*.PDF"))
    # Remove duplicates (rglob may return both if filesystem is case-sensitive)
    seen_paths: set[Path] = set()
    unique_pdfs: list[Path] = []
    for p in pdf_files:
        resolved = p.resolve()
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            unique_pdfs.append(p)

    # Apply the --limit cap after dedup so "limit=3" always means 3 real files.
    if limit is not None and limit > 0:
        unique_pdfs = unique_pdfs[:limit]

    total = len(unique_pdfs)
    if total == 0:
        print(f"No PDF files found in {folder}")
        return {**counts, "total": 0}

    print(f"Found {total} PDF file(s) in {folder}  (engine={engine})")

    # LLMConfigError lives in llm_extractor; import lazily so the regex path
    # doesn't need the anthropic SDK to be installed.
    if engine == "llm":
        from njlead.ingest.llm_extractor import LLMConfigError
    else:
        LLMConfigError = ()  # type: ignore[assignment]  # never matches an except

    for i, pdf_path in enumerate(unique_pdfs, start=1):
        print(f"  [{i}/{total}] {pdf_path.name} ...", end=" ", flush=True)
        try:
            with get_session() as session:
                result = _ingest_one(session, pdf_path, engine=engine)
        except LLMConfigError:
            # Missing key / missing SDK — no file will succeed. Re-raise so the
            # CLI can print a friendly setup message and exit non-zero.
            print("ABORT")
            raise
        except Exception as e:
            # Catch-all: something unexpected happened outside _ingest_one
            print(f"ERROR: {e}")
            counts["failed"] += 1
            continue

        counts[result] += 1
        print(result)

    counts["total"] = total
    return counts
