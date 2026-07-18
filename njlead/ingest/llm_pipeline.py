"""
LLM ingest path - orchestrates one report through the AI engine.

Given a document's pages, this:
  1. calls the LLM once (llm_extractor.extract_report),
  2. runs every returned row through the validation gate, and
  3. writes accepted rows to samples/measurements; logs held rows and
     report-level problems to the issues table for human review.

It reuses the same school de-duplication and issue-logging helpers as the regex
path, so both engines write identical database shapes. Only the extraction step
differs.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from njlead.db.models import Document, Measurement, Sample


def ingest_report_llm(
    session: Session, doc: Document, pages: list[dict], pdf_path: Path
) -> tuple[int, bool]:
    """
    Run one report through the LLM engine and write the results.

    Returns (measurements_written, has_issues).

    Raises LLMConfigError (no API key) - the caller aborts the run with
    setup instructions rather than failing every file silently.
    """
    # Lazy imports avoid a circular import with loader and keep the SDK optional.
    from njlead.ingest.loader import _get_or_create_school, _log_issue
    from njlead.ingest.llm_extractor import extract_report, LLMExtractionError
    from njlead.ingest.validation import validate_report

    # extract_report may raise LLMConfigError (propagate) or LLMExtractionError.
    try:
        report = extract_report(pages, pdf_path)
    except LLMExtractionError as e:
        _log_issue(session, doc, "llm_error", str(e))
        return 0, True

    # Source-span verification needs extracted text. Pure scans have none, so we
    # skip that one check for image-only reports and rely on range/unit/confidence.
    full_text = "\n".join((p.get("raw_text") or "") for p in pages)
    text_available = len(full_text.strip()) >= 30

    accepted, held = validate_report(
        report.measurements, full_text, check_source_span=text_available
    )

    school = _get_or_create_school(session, report.school_name, report.district)
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
        _log_issue(
            session, doc, "needs_review",
            f"Row held by validation ({who}): " + "; ".join(reasons),
        )

    has_issues = bool(held)
    if report.school_name is None:
        _log_issue(session, doc, "no_school_found", "LLM did not return a school name.")
        has_issues = True
    if written == 0 and not held:
        _log_issue(session, doc, "no_measurements", "LLM returned no measurements for this report.")
        has_issues = True

    return written, has_issues
