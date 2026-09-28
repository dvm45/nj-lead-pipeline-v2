"""
Page-type classifier — filters noise pages before LLM extraction.

Two-tier approach:
  1. Text pages: keyword heuristics (free, instant)
  2. Scanned pages: lightweight Haiku image classification (~$0.001/page)

Surveys of the NJ lead-testing PDF corpus show ~16% of pages are noise
(chain-of-custody forms, cover letters, lab boilerplate). For text pages
this is detectable by keywords alone. For scanned pages (42% of corpus),
a cheap Haiku call classifies the image before the expensive Opus
extraction ever sees it.
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path

import fitz  # PyMuPDF — for rendering scanned pages to images

from njlead import config

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword sets (text-page classification)
# ---------------------------------------------------------------------------

_DATA_KEYWORDS = re.compile(
    r"\b("
    r"ppb|ug/l|µg/l|mg/l|ppm"
    r"|result|lead\s+concentration|action\s+level"
    r"|sample\s+id|sample\s+location|sample\s+point"
    r"|fixture|first\s+draw|flush|faucet|fountain|water\s+cooler|bubbler"
    r"|non[- ]?detect|nd\b"
    r"|outlet|spigot|sink|tap"
    r")\b",
    re.IGNORECASE,
)

_NUMERIC_RESULT_RE = re.compile(r"(?:<\s*)\d+(?:\.\d+)?|\d+\.\d{1,4}")

_COC_KEYWORDS = re.compile(
    r"\b("
    r"chain\s+of\s+custody"
    r"|sample\s+receipt"
    r"|relinquished\s+by"
    r"|received\s+by"
    r"|sample\s+login"
    r"|cooler\s+temp"
    r"|custody\s+record"
    r"|coc\s+number"
    r"|coc\s+#"
    r")\b",
    re.IGNORECASE,
)

_COVER_LETTER_KEYWORDS = re.compile(
    r"(?:"
    r"^dear\s"
    r"|sincerely"
    r"|we\s+are\s+writing"
    r"|pleased\s+to\s+inform"
    r"|to\s+whom\s+it\s+may\s+concern"
    r"|enclosed\s+(?:please\s+find|are)"
    r"|attached\s+(?:please\s+find|are)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

_LAB_BOILERPLATE_KEYWORDS = re.compile(
    r"\b("
    r"accreditation"
    r"|certification"
    r"|nelap"
    r"|nelac"
    r"|iso\s+17025"
    r"|this\s+report\s+shall\s+not"
    r"|unless\s+otherwise\s+noted"
    r"|quality\s+assurance"
    r"|analytical\s+methods?\s+summary"
    r")\b",
    re.IGNORECASE,
)

_DIVIDER_KEYWORDS = re.compile(
    r"\b(ATTACHMENT|APPENDIX|TABLE\s+OF\s+CONTENTS)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text-page classifier (keyword heuristic, no LLM)
# ---------------------------------------------------------------------------

def classify_page(raw_text: str, page_num: int) -> str:
    """
    Classify a text page by keywords.

    Returns: 'data', 'keep', 'coc', 'cover', 'boilerplate', or 'divider'.
    """
    text = raw_text.strip()

    if page_num == 0:
        return "keep"

    data_matches = len(_DATA_KEYWORDS.findall(text))
    has_numeric = bool(_NUMERIC_RESULT_RE.search(text))
    coc_matches = len(_COC_KEYWORDS.findall(text))
    cover_match = bool(_COVER_LETTER_KEYWORDS.search(text))
    boilerplate_matches = len(_LAB_BOILERPLATE_KEYWORDS.findall(text))

    if data_matches >= 2:
        return "data"
    if data_matches >= 1 and has_numeric:
        return "data"

    if coc_matches >= 1 and data_matches == 0:
        return "coc"
    if cover_match and data_matches == 0:
        return "cover"
    if boilerplate_matches >= 2 and data_matches == 0:
        return "boilerplate"
    if len(text) < 300 and _DIVIDER_KEYWORDS.search(text) and data_matches == 0:
        return "divider"

    if len(text) < 30 and data_matches == 0:
        return "divider"

    if has_numeric and len(text) > 200:
        return "data"

    return "data"


# ---------------------------------------------------------------------------
# Scanned-page classifier (Haiku image call)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = (
    "Look at this scanned page from a lead-in-water testing report. "
    "Classify it as ONE of these types:\n"
    "  data_table — contains a table of lead test results (sample IDs, "
    "locations, ppb values, action levels)\n"
    "  chain_of_custody — a COC form (sample receipt, relinquished/received "
    "by, cooler temp, custody record)\n"
    "  cover_letter — a letter (Dear..., Sincerely, notification about "
    "testing)\n"
    "  boilerplate — lab certifications, disclaimers, accreditation info\n"
    "  other — title page, appendix divider, or anything else without "
    "lead test data\n\n"
    "Reply with ONLY the type name, nothing else."
)

_HAIKU_MODEL_BEDROCK = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_HAIKU_MODEL_ANTHROPIC = "claude-haiku-4-5"
_CLASSIFY_DPI = 100  # lower than extraction DPI — just need to read the layout


def _render_page_b64(pdf_path: Path, page_num: int) -> str:
    """Render a PDF page to a base64 PNG at classification DPI."""
    doc = fitz.open(str(pdf_path))
    try:
        pix = doc[page_num].get_pixmap(dpi=_CLASSIFY_DPI)
        return base64.b64encode(pix.tobytes("png")).decode("ascii")
    finally:
        doc.close()


def _get_classifier_client():
    """Build a client + model_id for the Haiku classifier."""
    try:
        import anthropic
    except ImportError:
        return None, None

    if config.LLM_PROVIDER == "bedrock":
        try:
            client = anthropic.AnthropicBedrock(aws_region=config.AWS_REGION)
        except Exception:
            return None, None
        return client, _HAIKU_MODEL_BEDROCK
    else:
        if not config.ANTHROPIC_API_KEY:
            return None, None
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        return client, _HAIKU_MODEL_ANTHROPIC


def classify_scanned_page(
    client, model_id: str, pdf_path: Path, page_num: int
) -> str:
    """
    Classify a scanned page by sending its image to Haiku.

    Returns: 'data_table', 'chain_of_custody', 'cover_letter',
             'boilerplate', or 'other'.
    On error, returns 'data_table' (safe fallback — don't drop pages we
    can't classify).
    """
    try:
        b64 = _render_page_b64(pdf_path, page_num)
        message = client.messages.create(
            model=model_id,
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _CLASSIFY_PROMPT},
                ],
            }],
        )
        label = message.content[0].text.strip().lower().replace(" ", "_")
        if label in ("data_table", "chain_of_custody", "cover_letter", "boilerplate", "other"):
            return label
        return "data_table"
    except Exception as e:
        _log.debug("Haiku classify failed for %s page %d: %s", pdf_path.name, page_num, e)
        return "data_table"


# ---------------------------------------------------------------------------
# Combined filter
# ---------------------------------------------------------------------------

def filter_noise_pages(
    pages: list[dict],
    pdf_path: Path | None = None,
    classify_images: bool = True,
) -> tuple[list[dict], int]:
    """
    Filter a report's page list, removing noise pages.

    Text pages are classified by keyword heuristics (free).
    Scanned pages are optionally classified by Haiku image calls.
    Page 0 is always kept regardless of classification.

    Args:
        pages: list of page dicts with keys page_num, raw_text, is_blank
        pdf_path: path to the PDF (needed for rendering scanned pages)
        classify_images: if True and credentials are available, use Haiku
            to classify scanned pages. If False, all scanned pages are kept.

    Returns:
        (filtered_pages, dropped_count)
    """
    kept: list[dict] = []
    dropped = 0

    # Lazy-init the Haiku client only if there are scanned pages to classify
    haiku_client = None
    haiku_model = None
    haiku_tried = False

    for pg in pages:
        # Always keep page 0
        if pg["page_num"] == 0:
            kept.append(pg)
            continue

        is_blank = pg.get("is_blank") or not (pg.get("raw_text") or "").strip()

        if not is_blank:
            # Text page — keyword classification
            label = classify_page(pg.get("raw_text", ""), pg["page_num"])
            if label in ("data", "keep"):
                kept.append(pg)
            else:
                dropped += 1
                _log.debug("Dropped text page %d (%s): %s", pg["page_num"], label, pdf_path)
            continue

        # Scanned page — try Haiku classification if enabled
        if not classify_images or pdf_path is None:
            kept.append(pg)
            continue

        if not haiku_tried:
            haiku_tried = True
            haiku_client, haiku_model = _get_classifier_client()
            if haiku_client is None:
                _log.debug("No Haiku client available; keeping all scanned pages")

        if haiku_client is None:
            kept.append(pg)
            continue

        label = classify_scanned_page(haiku_client, haiku_model, pdf_path, pg["page_num"])
        if label == "data_table":
            kept.append(pg)
        else:
            dropped += 1
            _log.debug("Dropped scanned page %d (%s): %s", pg["page_num"], label, pdf_path)

    return kept, dropped
