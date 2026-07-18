"""
LLM extraction client - the single API call that replaces the regex parsers.

For one report it builds a request (instructions + our schema + the page content,
text or image), sends it to Claude, and returns a validated-by-Pydantic
ExtractedReport. Structured output is enforced with a "tool" whose input schema
IS our Pydantic schema, so the model can only answer in our exact shape.

The API key is read from njlead.config (environment / .env). Nothing here runs
until a key is present: extract_report() raises LLMConfigError with a clear
message if the key is missing, so you can wire everything up and add the key later.
"""

from __future__ import annotations

import base64
from pathlib import Path

import fitz  # PyMuPDF - used only to rasterize scanned pages

from njlead import config
from njlead.ingest.schema import ExtractedReport

# Name of the structured-output tool we force the model to call.
_TOOL_NAME = "record_lead_report"

_SYSTEM_PROMPT = (
    "You extract lead-in-water testing results from New Jersey school reports. "
    "You are given one report, which may be typed text or a scanned image, from "
    "any of many different labs. Read it the way a careful analyst would and call "
    "the record_lead_report tool exactly once to return the data in the required "
    "form.\n\n"
    "Rules:\n"
    "- Fill only what the report actually states. Use null for anything missing; "
    "never guess.\n"
    "- Capture every water sample that has a lead result, including non-detects.\n"
    "- result_ppb must be the lead value in ppb (= ug/L). 'ND' / 'None Detected' "
    "-> 0.0; '<1.00' -> the detection limit (1.0). Put the value exactly as "
    "printed in result_raw, and the printed unit in unit_raw.\n"
    "- source_text must be the verbatim line(s) the result was read from.\n"
    "- lab_name is the lab/vendor on the letterhead (e.g. EMSL, LEW, RAMM); it is "
    "just a field, not something you route on.\n"
    "- confidence is your own 0-1 confidence in each row."
)


class LLMConfigError(RuntimeError):
    """Raised when an LLM call is attempted without an API key configured."""


class LLMExtractionError(RuntimeError):
    """Raised when the API call or response parsing fails for a report."""


def _page_image_block(pdf_path: Path, page_num: int) -> dict:
    """Render one PDF page to a base64 PNG image content block."""
    doc = fitz.open(str(pdf_path))
    try:
        page = doc[page_num]
        pix = page.get_pixmap(dpi=config.SCAN_RENDER_DPI)
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }


def _build_content(pages: list[dict], pdf_path: Path) -> list[dict]:
    """
    Build the user message content from a report's pages.

    `pages` is a list of dicts: {page_num, raw_text, is_blank}. Text pages are
    sent as text; blank (scanned) pages are rasterized and sent as images.
    """
    content: list[dict] = [
        {"type": "text", "text": "Here is one lead-testing report. Extract all results."}
    ]
    for pg in pages:
        n = pg["page_num"]
        if pg.get("is_blank") or not (pg.get("raw_text") or "").strip():
            content.append({"type": "text", "text": f"--- Page {n + 1} (scanned image) ---"})
            content.append(_page_image_block(pdf_path, n))
        else:
            content.append({"type": "text", "text": f"--- Page {n + 1} ---\n{pg['raw_text']}"})
    return content


def extract_report(pages: list[dict], pdf_path: Path) -> ExtractedReport:
    """
    Run one API call for a single report and return a parsed ExtractedReport.

    Raises:
      LLMConfigError     if no API key is configured (add it, then re-run).
      LLMExtractionError if the call fails or the response can't be parsed.
    """
    if not config.api_key_is_set():
        raise LLMConfigError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY in your environment "
            "or in a .env file (copy .env.example to .env), then re-run. "
            "Check status any time with:  njlead check-llm"
        )

    # Import the SDK lazily so the rest of the pipeline works without it installed.
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMConfigError(
            "The 'anthropic' package is not installed. Run:  pip install -r requirements.txt"
        ) from e

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    tool = {
        "name": _TOOL_NAME,
        "description": "Return the extracted lead-testing data for one report.",
        "input_schema": ExtractedReport.model_json_schema(),
    }

    try:
        message = client.messages.create(
            model=config.LLM_MODEL,
            max_tokens=config.LLM_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": _build_content(pages, pdf_path)}],
        )
    except Exception as e:
        raise LLMExtractionError(f"API call failed: {e}") from e

    # Pull the forced tool_use block and validate it against our schema.
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
            try:
                return ExtractedReport.model_validate(block.input)
            except Exception as e:
                raise LLMExtractionError(f"Response did not match schema: {e}") from e

    raise LLMExtractionError("Model did not return the expected structured output.")
