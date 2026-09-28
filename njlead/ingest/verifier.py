"""
Sonnet verification agent — cross-checks Opus extraction against source pages.

This is Agent 3 in the multi-agent pipeline:
  Agent 1: Haiku page classifier (filters noise pages)
  Agent 2: Opus extractor (extracts structured data)
  Agent 3: Sonnet verifier (this module — confirms extraction accuracy)

The verifier receives the extractor's output alongside a SUMMARY of the
source content and checks:
  - Does each extracted value look plausible given the source?
  - Are the school/district metadata correct?
  - Is this a multi-school report where rows are misattributed?
  - Are any values clearly wrong (zip codes as ppb, duplicates)?

Cost-optimized: only the first page (metadata) + a text digest of data
pages are sent — NOT all source pages as images. For reports the extractor
handled confidently (single-school, few pages, high scores), verification
is skipped entirely.
"""

from __future__ import annotations

import logging
from pathlib import Path

from njlead import config
from njlead.ingest.schema import ExtractedMeasurement, ExtractedReport

_log = logging.getLogger(__name__)

_SONNET_MODEL_BEDROCK = "us.anthropic.claude-sonnet-4-6"
_SONNET_MODEL_ANTHROPIC = "claude-sonnet-4-6"

# --- Conditional verification thresholds ---
_SKIP_MAX_PAGES = 6
_SKIP_MIN_AVG_CONFIDENCE = 0.85
_SKIP_MAX_MEASUREMENTS = 50

_VERIFY_SYSTEM = (
    "You are a data verification agent for lead-in-water testing reports from "
    "New Jersey schools. You receive extracted data alongside source page text "
    "and metadata. Your job is to verify the extraction is accurate and complete.\n\n"
    "You are NOT re-extracting — you are auditing someone else's extraction. "
    "Focus on: (1) whether school attribution is correct, especially for "
    "multi-school reports, (2) whether values match the source, (3) whether "
    "measurements were missed."
)

_VERIFY_PROMPT = """\
Below is the extracted data from a lead-in-water report, plus a digest of \
the source pages. Verify the extraction:

1. SCHOOL ATTRIBUTION: Is this a multi-school report? If so, are rows \
attributed to the correct school/building? This is the most common error.
2. VALUE CHECK: Do extracted result_ppb values match the source page text?
3. COMPLETENESS: Based on the source page count and text, does the number \
of extracted measurements seem reasonable? Flag if significantly low.
4. QUALITY FLAGS: Any zip codes/phone numbers parsed as ppb, duplicates, \
impossible values?

=== EXTRACTED DATA ===
Report-level school: {school_name}
Report-level district: {district}
Lab: {lab_name}
Measurements extracted: {meas_count}
Unique per-row schools found: {unique_schools}

{meas_summary}

=== SOURCE DIGEST ===
Total pages in report: {total_pages}
First page (metadata):
{first_page_text}

Data page summary (text excerpts, max 2000 chars):
{data_digest}

Reply by calling the verify_extraction tool with your findings."""


def _build_verify_tool_schema() -> dict:
    return {
        "name": "verify_extraction",
        "description": "Report verification findings for a lead-testing report extraction.",
        "input_schema": {
            "type": "object",
            "properties": {
                "values_correct": {
                    "type": "boolean",
                    "description": "True if extracted values appear consistent with source text.",
                },
                "is_multi_school": {
                    "type": "boolean",
                    "description": "True if the source covers more than one school/building.",
                },
                "school_attribution_correct": {
                    "type": "boolean",
                    "description": "True if rows are attributed to the correct schools. False if a multi-school report has all rows under one school.",
                },
                "school_name_correct": {
                    "type": "boolean",
                    "description": "True if the report-level school_name is reasonable.",
                },
                "school_name_suggested": {
                    "type": "string",
                    "description": "If school_name_correct is false, provide ONLY a corrected name (max 60 chars, no explanations). Null otherwise.",
                    "maxLength": 60,
                },
                "district_correct": {
                    "type": "boolean",
                    "description": "True if district matches the report.",
                },
                "district_suggested": {
                    "type": "string",
                    "description": "If district_correct is false, provide ONLY a corrected name (max 60 chars, no explanations). Null otherwise.",
                    "maxLength": 60,
                },
                "missed_count": {
                    "type": "integer",
                    "description": "Estimated number of lead results visible in source but NOT extracted. 0 if extraction looks complete.",
                },
                "flagged_rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sample_id": {"type": "string"},
                            "issue": {
                                "type": "string",
                                "maxLength": 120,
                                "description": "Brief issue description (max 120 chars).",
                            },
                            "corrected_ppb": {"type": "number"},
                        },
                        "required": ["sample_id", "issue"],
                    },
                    "description": "Extracted rows with errors. Keep brief.",
                },
                "overall_confidence": {
                    "type": "number",
                    "description": "Your confidence in the overall extraction quality, 0-1.",
                },
            },
            "required": [
                "values_correct",
                "is_multi_school",
                "school_attribution_correct",
                "school_name_correct",
                "district_correct",
                "missed_count",
                "flagged_rows",
                "overall_confidence",
            ],
        },
    }


def _format_meas_summary(measurements: list[ExtractedMeasurement]) -> str:
    lines = []
    for i, m in enumerate(measurements):
        loc = m.sample_location or m.sample_id or "?"
        school = f"  school={m.school_name}" if m.school_name else ""
        draw = f"  draw={m.draw_type}" if m.draw_type else ""
        lines.append(
            f"  [{i+1}] {loc}: {m.result_raw} -> {m.result_ppb} ppb"
            f"{school}{draw}"
        )
    return "\n".join(lines) if lines else "  (none)"


def _build_source_digest(pages: list[dict], pdf_path: Path) -> tuple[str, str]:
    """
    Build a compact text digest of the source pages instead of sending
    all pages as images. Returns (first_page_text, data_digest).
    """
    first_page = ""
    data_lines: list[str] = []
    char_budget = 2000

    for pg in pages:
        text = (pg.get("raw_text") or "").strip()
        if pg["page_num"] == 0:
            first_page = text[:800] if text else "(scanned — no text available)"
            continue
        if not text:
            data_lines.append(f"[Page {pg['page_num']+1}: scanned image, no text]")
            continue
        remaining = char_budget - sum(len(l) for l in data_lines)
        if remaining <= 0:
            data_lines.append(f"... and {len(pages) - len(data_lines) - 1} more pages")
            break
        snippet = text[:min(300, remaining)]
        data_lines.append(f"[Page {pg['page_num']+1}] {snippet}")

    if not first_page:
        first_page = "(scanned — no text available)"

    return first_page, "\n".join(data_lines) if data_lines else "(no text pages)"


def _get_verifier_client():
    """Build a Sonnet client for verification."""
    try:
        import anthropic
    except ImportError:
        return None, None

    if config.LLM_PROVIDER == "bedrock":
        try:
            client = anthropic.AnthropicBedrock(aws_region=config.AWS_REGION)
        except Exception:
            return None, None
        return client, _SONNET_MODEL_BEDROCK
    else:
        if not config.ANTHROPIC_API_KEY:
            return None, None
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        return client, _SONNET_MODEL_ANTHROPIC


def should_verify(
    report: ExtractedReport,
    pages: list[dict],
) -> bool:
    """
    Decide whether this report needs Sonnet verification.

    Skip verification for high-confidence, single-school, short reports
    where the extractor is unlikely to have made errors worth catching.
    """
    if not report.measurements:
        return False

    n_pages = len(pages)
    n_meas = len(report.measurements)

    has_per_row_schools = any(m.school_name for m in report.measurements)
    if has_per_row_schools:
        return True

    avg_conf = sum(m.confidence for m in report.measurements) / n_meas

    if (
        n_pages <= _SKIP_MAX_PAGES
        and avg_conf >= _SKIP_MIN_AVG_CONFIDENCE
        and n_meas <= _SKIP_MAX_MEASUREMENTS
    ):
        return False

    return True


def verify_report(
    report: ExtractedReport,
    pages: list[dict],
    pdf_path: Path,
) -> dict | None:
    """
    Run Sonnet verification on an Opus extraction.

    Returns a dict with verification findings, or None if verification
    couldn't run or was skipped.
    """
    client, model_id = _get_verifier_client()
    if client is None:
        _log.debug("No Sonnet client available; skipping verification")
        return None

    meas_summary = _format_meas_summary(report.measurements)
    first_page_text, data_digest = _build_source_digest(pages, pdf_path)

    unique_schools = set()
    for m in report.measurements:
        if m.school_name:
            unique_schools.add(m.school_name)
    unique_schools_str = ", ".join(sorted(unique_schools)) if unique_schools else "(all under report-level school)"

    prompt_text = _VERIFY_PROMPT.format(
        school_name=report.school_name or "(not extracted)",
        district=report.district or "(not extracted)",
        lab_name=report.lab_name or "(not extracted)",
        meas_count=len(report.measurements),
        unique_schools=unique_schools_str,
        meas_summary=meas_summary,
        total_pages=len(pages),
        first_page_text=first_page_text,
        data_digest=data_digest,
    )

    user_content = [{"type": "text", "text": prompt_text}]

    system_blocks = [
        {
            "type": "text",
            "text": _VERIFY_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    tool = _build_verify_tool_schema()
    cached_tool = dict(tool)
    cached_tool["cache_control"] = {"type": "ephemeral"}

    try:
        message = client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=system_blocks,
            tools=[cached_tool],
            tool_choice={"type": "tool", "name": "verify_extraction"},
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as e:
        _log.warning("Verification call failed: %s", e)
        return None

    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "verify_extraction":
            result = block.input if isinstance(block.input, dict) else {}
            in_tok = message.usage.input_tokens
            out_tok = message.usage.output_tokens
            missed = result.get("missed_count", 0)
            flagged = len(result.get("flagged_rows", []))
            overall = result.get("overall_confidence", 1.0)
            multi = result.get("is_multi_school", False)
            attr_ok = result.get("school_attribution_correct", True)
            print(
                f"    [verify] sonnet in={in_tok} out={out_tok}  "
                f"multi_school={multi}  attribution_ok={attr_ok}  "
                f"missed={missed}  flagged={flagged}  "
                f"confidence={overall:.2f}",
                flush=True,
            )
            return result

    _log.warning("Verification returned no tool call")
    return None


def apply_verification(
    report: ExtractedReport,
    findings: dict,
) -> tuple[ExtractedReport, list[str]]:
    """
    Apply verification findings to adjust the extraction.

    Returns (possibly-modified report, list of issue descriptions).
    """
    issues: list[str] = []

    # Fix school name if verifier corrected it (schema-constrained to 60 chars)
    if not findings.get("school_name_correct") and findings.get("school_name_suggested"):
        suggested = findings["school_name_suggested"].strip()
        if 2 < len(suggested) <= 60:
            old = report.school_name
            report.school_name = suggested
            issues.append(f"School name corrected: '{old}' -> '{report.school_name}'")

    # Fix district if verifier corrected it
    if not findings.get("district_correct") and findings.get("district_suggested"):
        suggested = findings["district_suggested"].strip()
        if 2 < len(suggested) <= 60:
            old = report.district
            report.district = suggested
            issues.append(f"District corrected: '{old}' -> '{report.district}'")

    # Multi-school misattribution — downgrade all rows when the verifier
    # says it's multi-school but rows aren't properly attributed
    is_multi = findings.get("is_multi_school", False)
    attr_ok = findings.get("school_attribution_correct", True)
    if is_multi and not attr_ok:
        for m in report.measurements:
            m.confidence = min(m.confidence, 0.50)
        issues.append(
            "Multi-school report with incorrect school attribution; "
            "all rows downgraded to 0.50 confidence"
        )

    # Adjust confidence on flagged rows
    flagged_ids = {r.get("sample_id") for r in findings.get("flagged_rows", [])}
    for m in report.measurements:
        if m.sample_id in flagged_ids:
            m.confidence = min(m.confidence, 0.5)

    # Downgrade overall confidence if verifier is uncertain
    overall = findings.get("overall_confidence", 1.0)
    if overall < 0.8:
        for m in report.measurements:
            m.confidence = min(m.confidence, overall)
        issues.append(f"Verifier overall confidence low ({overall:.2f}); all rows downgraded")

    # Log missed measurements
    missed = findings.get("missed_count", 0)
    if missed > 0:
        issues.append(f"Verifier estimates {missed} measurement(s) missed by extractor")

    # Log flagged rows (brief — schema constrains issue to 120 chars)
    for row in findings.get("flagged_rows", []):
        sid = row.get("sample_id", "?")
        issue = row.get("issue", "unknown issue")
        issues.append(f"Flagged row {sid}: {issue}")

    return report, issues
