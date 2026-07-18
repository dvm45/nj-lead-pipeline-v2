"""
Ingest orchestrator — the engine behind `njlead ingest <folder>`.

For each PDF found in the target folder, this module:
  1. Computes a SHA-256 fingerprint → skips the file if already ingested
  2. Calls extractor.py to pull raw text out of each page
  3. Stores the raw text in the `pages` table (so you can inspect it later
     with /show-sample even if extraction found nothing useful)
  4. Hands the whole report to the LLM pipeline for one API call
  5. Prints a summary at the end

Design note: individual-file errors are caught and counted, so one bad PDF
never aborts a batch run. The one exception is LLMConfigError (missing API
key or SDK): that repeats on every file, so we let it bubble up and the
caller aborts the run with setup instructions.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from njlead.db.models import Document, Page
from njlead.db.session import get_session
from njlead.ingest.extractor import extract_pdf


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
# Single-file ingest
# ---------------------------------------------------------------------------

def _ingest_one(session: Session, path: Path) -> str:
    """
    Ingest one PDF file through the LLM pipeline.

    Returns one of: 'ingested', 'skipped', 'failed'
    """
    # Lazy import — keeps a circular dependency loose and avoids importing
    # the anthropic SDK until we actually need it.
    from njlead.ingest.llm_pipeline import ingest_report_llm, log_issue

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
        log_issue(session, doc, "extraction_failed", extracted.error)
        session.commit()
        return "failed"

    # --- Step 4: Store raw page text ---
    # Kept even when we're going to send images to the model, so /show-sample
    # still works for debugging and so the model's output can be traced back
    # to the source text later.
    for extracted_page in extracted.pages:
        page_row = Page(
            document_id=doc.id,
            page_num=extracted_page.page_num,
            raw_text=extracted_page.raw_text,
            is_blank=extracted_page.is_blank,
        )
        session.add(page_row)

    # --- Step 5: Hand the report to the LLM pipeline ---
    # The LLM pipeline builds a simple list of page dicts (page_num / raw_text
    # / is_blank) so its interface doesn't depend on our ExtractedPage class.
    pages_payload = [
        {
            "page_num": p.page_num,
            "raw_text": p.raw_text,
            "is_blank": p.is_blank,
        }
        for p in extracted.pages
    ]

    # LLMConfigError (missing key / SDK) is deliberately NOT caught here — it
    # would repeat on every file, so it propagates and the caller aborts the
    # whole run with setup instructions.
    total_measurements, has_issues = ingest_report_llm(
        session, doc, pages_payload, path
    )

    # If we processed pages but the model found zero measurements
    if total_measurements == 0 and not extracted.failed:
        log_issue(
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
# Folder walk — the public entry point
# ---------------------------------------------------------------------------

def ingest_folder(folder: Path, limit: int | None = None) -> dict[str, int]:
    """
    Walk a folder recursively and ingest every PDF file found.

    limit:  process at most this many files. Handy for a cheap first run
            (e.g. `njlead ingest data/ --limit 3`) before committing to the
            full set.

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

    print(f"Found {total} PDF file(s) in {folder}")

    # Import LLMConfigError lazily so this module doesn't require the anthropic
    # SDK to even be importable — the CLI's `check-llm` still works without it.
    from njlead.ingest.llm_extractor import LLMConfigError

    for i, pdf_path in enumerate(unique_pdfs, start=1):
        print(f"  [{i}/{total}] {pdf_path.name} ...", end=" ", flush=True)
        try:
            with get_session() as session:
                result = _ingest_one(session, pdf_path)
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
