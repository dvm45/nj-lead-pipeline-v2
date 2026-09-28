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

import logging
import re

from rapidfuzz import fuzz

_log = logging.getLogger(__name__)

_SCHOOL_NUM_RE = re.compile(r"\bp\.?\s*s\.?\s*#?\s*(\d{1,3})\b", re.IGNORECASE)
_SCHOOL_NUM_RE2 = re.compile(r"\bschool\s*(?:no\.?\s*)?#?\s*(\d{1,3})\b", re.IGNORECASE)

def _school_number(name: str | None) -> int | None:
    if not name:
        return None
    for pat in (_SCHOOL_NUM_RE, _SCHOOL_NUM_RE2):
        m = pat.search(name)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


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

    if not name:
        return None

    name = name.strip().title() if name else None
    district = district.strip().title() if district else None

    existing = (
        session.query(School)
        .filter(School.name == name, School.district == district)
        .first()
    )
    if existing:
        return existing

    # Fuzzy dedup: before creating a new row, check if a similar school
    # already exists in the same district.
    if district:
        same_district = session.query(School).filter(School.district == district).all()
    else:
        same_district = session.query(School).filter(School.district.is_(None)).all()

    input_num = _school_number(name)
    best_match: School | None = None
    best_score = 0.0

    for candidate in same_district:
        cand_num = _school_number(candidate.name)
        # Same school number = identity match (e.g. "PS #10" and "Public School No. 10")
        if input_num is not None and cand_num is not None and input_num == cand_num:
            _log.info("Fuzzy dedup (number match): '%s' → existing '%s' (district=%s)", name, candidate.name, district)
            return candidate
        score = fuzz.WRatio(name or "", candidate.name or "")
        if score > best_score:
            best_score = score
            best_match = candidate

    if best_match is not None and best_score >= 88:
        _log.info("Fuzzy dedup (score=%.1f): '%s' → existing '%s' (district=%s)", best_score, name, best_match.name, district)
        return best_match

    school = School(name=name, district=district, state="NJ")
    session.add(school)
    session.flush()
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
    from njlead.ingest.llm_extractor import extract_report, LLMExtractionError, parse_folder_hints
    from njlead.ingest.validation import validate_report
    from njlead.ingest.verifier import verify_report, apply_verification, should_verify

    # extract_report may raise LLMConfigError (propagate) or LLMExtractionError.
    try:
        report = extract_report(pages, pdf_path)
    except LLMExtractionError as e:
        log_issue(session, doc, "llm_error", str(e))
        return 0, True

    # Fall back to folder-path hints when the LLM didn't return a district.
    hints = parse_folder_hints(pdf_path)
    if not report.district and hints.get("district"):
        report.district = hints["district"]
    if not report.school_name and hints.get("school_name"):
        report.school_name = hints["school_name"]

    # --- Sonnet verification pass ---
    # Conditional: skip verification for high-confidence, single-school,
    # short reports where the extractor is unlikely to have made errors.
    # Always verify multi-school reports and large/low-confidence ones.
    if report.measurements and should_verify(report, pages):
        findings = verify_report(report, pages, pdf_path)
        if findings is not None:
            report, verify_issues = apply_verification(report, findings)
            for vi in verify_issues:
                log_issue(session, doc, "verification", vi)

    # Source-span verification needs extracted text. Skip it whenever ANY page
    # in this report was sent as an image (is_blank=True): the model may have
    # read the sample rows off that image, in which case the location and
    # value legitimately won't appear in the text stream we'd check against.
    # (Reports where the letterhead is text but the data table is a scan are
    # the common case that broke earlier — see Ringwood_Cooper_ES.)
    full_text = "\n".join((p.get("raw_text") or "") for p in pages)
    any_image_page = any(p.get("is_blank") for p in pages)
    text_available = len(full_text.strip()) >= 30 and not any_image_page

    accepted, held = validate_report(
        report.measurements, full_text, check_source_span=text_available
    )

    # Report-level school is used as the default for rows that don't carry
    # their own. Some district-wide PDFs return no header school but still
    # populate per-row school_name for every measurement.
    default_school = get_or_create_school(session, report.school_name, report.district)
    if default_school is not None and report.lab_name and not default_school.lab_name:
        default_school.lab_name = report.lab_name.strip()

    # Cache per-row schools within this report to avoid a query per measurement
    # when many rows share the same school.
    school_cache: dict[tuple[str | None, str | None], "School | None"] = {}
    if default_school is not None:
        school_cache[(report.school_name, report.district)] = default_school

    def _resolve_school(row_school: str | None, row_district: str | None):
        # A row's school_name/district override the report header when set.
        # Fall back to the report header, and finally to None.
        name = row_school or report.school_name
        district = row_district or report.district
        key = (name, district)
        if key in school_cache:
            return school_cache[key]
        s = get_or_create_school(session, name, district)
        if s is not None and report.lab_name and not s.lab_name:
            s.lab_name = report.lab_name.strip()
        school_cache[key] = s
        return s

    written = 0
    for m in accepted:
        row_school = _resolve_school(m.school_name, m.district)
        sample = Sample(
            document_id=doc.id,
            school_id=row_school.id if row_school else None,
            sample_id=m.sample_id,
            location=m.sample_location,
            fixture_type=m.fixture_type,
            draw_type=getattr(m, "draw_type", None),
            sample_date=m.sample_date,
            test_year=m.test_year,
        )
        session.add(sample)
        session.flush()

        exceeds = None
        if m.action_level_ppb is not None:
            exceeds = m.result_ppb > m.action_level_ppb
        # Citation fields: the extractor stashes a (lo, hi) page range on
        # each measurement instance via object.__setattr__. We keep the high
        # end as the canonical source_page (a reviewer opening that page will
        # find the row's source_text on it or a couple pages earlier).
        source_page = getattr(m, "_source_page_hi", None)
        session.add(
            Measurement(
                sample_id=sample.id,
                analyte=m.analyte,
                result_ppb=m.result_ppb,
                action_level_ppb=m.action_level_ppb,
                exceeds_action_level=exceeds,
                source_text=m.source_text,
                confidence=m.confidence,
                source_page=source_page,
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
    # A district-wide PDF may have no header school but a per-row school on
    # every measurement — that's fine. Only flag "no school" when NOTHING
    # resolved to a school.
    any_row_had_school = any(
        (m.school_name or report.school_name) for m in accepted
    )
    if not any_row_had_school and (report.school_name is None):
        log_issue(session, doc, "no_school_found", "LLM did not return a school name.")
        has_issues = True
    if written == 0 and not held:
        log_issue(session, doc, "no_measurements", "LLM returned no measurements for this report.")
        has_issues = True

    return written, has_issues
