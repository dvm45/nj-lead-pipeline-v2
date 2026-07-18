"""
LLM ingest path - orchestrates one report through the AI engine.

Given a document's pages, this:
  1. calls the LLM once (llm_extractor.extract_report),
  2. runs every returned row through the validation gate, and
  3. writes accepted rows to samples/measurements; logs held rows and
     report-level problems to the issues table for human review.

This module also owns the two small DB-write helpers (get_or_create_school,
log_issue) that used to live in loader.py. Keeping them here removes the
old circular import between loader and llm_pipeline: now loader imports from
llm_pipeline, and llm_pipeline imports from nothing in ingest except the
extractor + validation modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from njlead.db.models import Document, Issue, Measurement, Sample, School


# ---------------------------------------------------------------------------
# Small DB-write helpers, shared with loader.py
# ---------------------------------------------------------------------------

def get_or_create_school(
    session: Session, name: str | None, district: str | None
) -> School | None:
    """
    Look up a school by name + district, creating a new row if needed.

    Prevents duplicate school rows when the same school appears in many
    different PDF reports. Returns None if we don't have a usable name.
    """
    if not name and not district:
        return None

    # The schools table requires a name — if we only have a district,
    # skip creating the school row rather than crashing.
    if not name:
        return None

    # Normalize: strip whitespace, title-case for consistent de-dup
    name = name.strip().title() if name else None
    district = district.strip().title() if district else None

    existing = (
        session.query(School)
        .filter(School.name == name, School.district == district)
        .first()
    )
    if existing:
        return existing

    school = School(name=name, district=district, state="NJ")
    session.add(school)
    session.flush()  # assigns school.id without committing the full transaction
    return school


def log_issue(
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
# LLM ingest orchestrator
# ---------------------------------------------------------------------------

def ingest_report_llm(
    session: Session, doc: Document, pages: list[dict], pdf_path: Path
) -> tuple[int, bool]:
    """
    Run one report through the LLM engine and write the results.

    Returns (measurements_written, has_issues).

    Raises LLMConfigError (no API key) - the caller aborts the run with
    setup instructions rather than failing every file silently.
    """
    # Lazy import keeps the anthropic SDK optional at import time — the CLI's
    # check-llm command can still tell the user the SDK is missing even when
    # this module gets imported.
    from njlead.ingest.llm_extractor import extract_report, LLMExtractionError
    from njlead.ingest.validation import validate_report

    # extract_report may raise LLMConfigError (propagate) or LLMExtractionError.
    try:
        report = extract_report(pages, pdf_path)
    except LLMExtractionError as e:
        log_issue(session, doc, "llm_error", str(e))
        return 0, True

    # Source-span verification needs extracted text. Pure scans have none, so we
    # skip that one check for image-only reports and rely on range/unit/confidence.
    full_text = "\n".join((p.get("raw_text") or "") for p in pages)
    text_available = len(full_text.strip()) >= 30

    accepted, held = validate_report(
        report.measurements, full_text, check_source_span=text_available
    )

    school = get_or_create_school(session, report.school_name, report.district)
    # Persist the lab name as data (not a routing decision) if we learned one.
    if school is not None and report.lab_name and not school.lab_name:
        school.lab_name = report.lab_name.strip()

    written = 0
    for m in accepted:
        sample = Sample(
            document_id=doc.id,
            school_id=school.id if school else None,
            sample_id=m.sample_id,
            location=m.sample_location,
            fixture_type=m.fixture_type,
            sample_date=m.sample_date,
            test_year=m.test_year,
        )
        session.add(sample)
        session.flush()

        exceeds = None
        if m.action_level_ppb is not None:
            exceeds = m.result_ppb > m.action_level_ppb
        session.add(
            Measurement(
                sample_id=sample.id,
                analyte=m.analyte,
                result_ppb=m.result_ppb,
                action_level_ppb=m.action_level_ppb,
                exceeds_action_level=exceeds,
            )
        )
        written += 1

    # Held rows -> human review queue.
    for m, reasons in held:
        who = m.sample_id or m.sample_location or "unknown sample"
        log_issue(
            session, doc, "needs_review",
            f"Row held by validation ({who}): " + "; ".join(reasons),
        )

    has_issues = bool(held)
    if report.school_name is None:
        log_issue(session, doc, "no_school_found", "LLM did not return a school name.")
        has_issues = True
    if written == 0 and not held:
        log_issue(session, doc, "no_measurements", "LLM returned no measurements for this report.")
        has_issues = True

    return written, has_issues
