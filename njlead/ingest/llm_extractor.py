"""
LLM extraction client - the single API call that replaces the regex parsers.

For one report it builds a request (instructions + our schema + the page content,
text or image), sends it to Claude, and returns a validated-by-Pydantic
ExtractedReport. Structured output is enforced with a "tool" whose input schema
IS our Pydantic schema, so the model can only answer in our exact shape.

Two providers are supported (chosen via njlead.config.LLM_PROVIDER):

  - "bedrock"    -> anthropic.AnthropicBedrock, authed with AWS credentials,
                    region from AWS_REGION, model from BEDROCK_MODEL_ID
  - "anthropic"  -> anthropic.Anthropic, authed with ANTHROPIC_API_KEY,
                    model from ANTHROPIC_MODEL

Nothing here runs until credentials are present: extract_report() raises
LLMConfigError with a clear message if they are missing, so you can wire
everything up and add credentials later.
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
    "- source_text: a SHORT verbatim snippet (under 160 chars) - typically the "
    "location plus the result value, e.g. 'Water Cooler, 3rd Floor Hall Left  "
    "4.05'. Do NOT quote the whole row or the whole line if the row is long. "
    "This is an anti-hallucination check, not a display field.\n"
    "- lab_name is the lab/vendor on the letterhead (e.g. EMSL, LEW, RAMM); it is "
    "just a field, not something you route on.\n"
    "- confidence is your own 0-1 confidence in each row.\n"
    "- Always fill school_name and district when the report identifies them - "
    "these are usually on the cover page or in a header.\n"
    "- Some PDFs are DISTRICT-WIDE reports covering many schools in one file. "
    "When rows come from a school DIFFERENT from the report header, set the "
    "per-row school_name (and district if it differs) on that measurement. "
    "For single-school reports, leave the per-row school_name / district null "
    "- the report-level fields apply to every row."
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


def _build_client():
    """
    Build the right Anthropic SDK client for the configured provider.

    Returns a (client, model_id) tuple - the model ID differs between
    providers even for the same underlying Claude model (Anthropic uses
    "claude-sonnet-4-5"; Bedrock uses the full ARN-style ID with a region
    prefix like "us.anthropic.claude-sonnet-4-5-20250929-v1:0").
    """
    if not config.api_key_is_set():
        if config.LLM_PROVIDER == "bedrock":
            raise LLMConfigError(
                "No AWS credentials found for Bedrock. Set them up with one of:\n"
                "  - aws configure           (writes ~/.aws/credentials)\n"
                "  - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars\n"
                "  - an EC2/ECS instance role (when running on AWS)\n"
                "Check status any time with:  njlead check-llm"
            )
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

    if config.LLM_PROVIDER == "bedrock":
        # AnthropicBedrock reads AWS creds the same way boto3 does (env vars,
        # ~/.aws/credentials, or instance role). We only pass the region
        # explicitly so it doesn't default to us-east-1.
        try:
            client = anthropic.AnthropicBedrock(aws_region=config.AWS_REGION)
        except AttributeError as e:
            raise LLMConfigError(
                "This anthropic SDK version doesn't include AnthropicBedrock. "
                "Run: pip install -r requirements.txt"
            ) from e
        return client, config.BEDROCK_MODEL_ID

    # Default / "anthropic" path
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return client, config.ANTHROPIC_MODEL


# Reports beyond these sizes are split into page batches instead of a single
# call. The output-token cap is Sonnet 4.5's non-streaming ceiling; the input
# cap is a conservative estimate before Bedrock rejects with "Input is too long"
# (image-heavy scans hit this well before pure text does).
_BATCH_TRIGGER_PAGES_TEXT = 12    # split text-only reports > this many pages
_BATCH_TRIGGER_PAGES_IMAGE = 4    # split scanned reports > this many image pages
# Nominal batch size for text reports. 2 fits even dense district-wide
# reports (Paterson-style: ~30-40 measurements per page → ~15k output
# tokens for 2 pages, safely under the 16384 non-streaming cap). Larger
# text batches truncated on that corpus. If a batch still truncates,
# the recursive retry halves further and the adaptive-shrink logic
# downsizes the next batch.
_BATCH_SIZE_PAGES = 2
_BATCH_SIZE_IMAGE = 2             # nominal batch size when splitting scans


def _summarize_response(pages_batch: list[dict], message, label: str) -> tuple[int, bool, bool, dict, int]:
    """
    Print the per-call diagnostic block and return (measurements_count,
    truncated, empty_tool, tool_input_dict, out_tokens). Empty tool_input_dict
    = {} when no tool call was present.
    """
    pages_text = sum(
        1 for p in pages_batch
        if not p.get("is_blank") and (p.get("raw_text") or "").strip()
    )
    pages_image = len(pages_batch) - pages_text
    stop_reason = message.stop_reason
    truncated = stop_reason == "max_tokens"
    in_tokens = message.usage.input_tokens
    out_tokens = message.usage.output_tokens
    tool_input: dict = {}
    tool_present = False
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
            tool_present = True
            tool_input = block.input if isinstance(block.input, dict) else {}
            break
    meas = len(tool_input.get("measurements") or [])
    empty = tool_present and meas == 0

    flags: list[str] = []
    if truncated:
        flags.append("TRUNCATED (raise NJLEAD_LLM_MAX_TOKENS)")
    if empty:
        flags.append("EMPTY (model returned zero measurements)")
    if not tool_present:
        flags.append("NO_TOOL_CALL (model did not use the structured tool)")
    flag_str = f"  {' | '.join(flags)}" if flags else ""

    print(
        f"    [llm]{label} "
        f"pages={len(pages_batch)} (text={pages_text}, image={pages_image})  "
        f"stop={stop_reason}  in={in_tokens} out={out_tokens}  "
        f"measurements={meas}  "
        f"school={tool_input.get('school_name')!r}  lab={tool_input.get('lab_name')!r}"
        + flag_str,
        flush=True,
    )
    return meas, truncated, empty, tool_input, out_tokens


def _call_once(client, model_id: str, tool: dict, pages_batch: list[dict], pdf_path: Path) -> "tuple[dict, object]":
    """One raw API call for a page batch. Returns (tool_input_dict, message)."""
    message = client.messages.create(
        model=model_id,
        max_tokens=config.LLM_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        tools=[tool],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": _build_content(pages_batch, pdf_path)}],
    )
    return message


def _plan_batches(pages: list[dict]) -> list[list[dict]]:
    """
    Decide how to split a report's pages into batches. Reports below the
    trigger thresholds go through as a single batch (== today's behavior).
    Larger reports are chunked, with tighter chunks for image-heavy scans.
    """
    n = len(pages)
    if n == 0:
        return [pages]
    image_pages = sum(1 for p in pages if p.get("is_blank"))
    if image_pages > 0:
        if image_pages <= _BATCH_TRIGGER_PAGES_IMAGE:
            return [pages]
        size = _BATCH_SIZE_IMAGE
    else:
        if n <= _BATCH_TRIGGER_PAGES_TEXT:
            return [pages]
        size = _BATCH_SIZE_PAGES
    return [pages[i:i + size] for i in range(0, n, size)]


def extract_report(pages: list[dict], pdf_path: Path) -> ExtractedReport:
    """
    Run one API call for a single report (or several batched calls for very
    large reports) and return a parsed ExtractedReport.

    Large reports are split by page and merged: the header fields
    (school_name / district / lab_name) come from the first non-empty batch;
    measurements from all batches are concatenated. A batch that returns
    empty due to output truncation is retried once at half its page count.

    Raises:
      LLMConfigError     if credentials aren't configured for the selected
                         provider (add them, then re-run).
      LLMExtractionError if the call fails or the response can't be parsed.
    """
    client, model_id = _build_client()
    tool = {
        "name": _TOOL_NAME,
        "description": "Return the extracted lead-testing data for one report.",
        "input_schema": ExtractedReport.model_json_schema(),
    }

    # Queue of pending page batches. We pop from the front and may push
    # smaller sub-batches back when adaptive shrinking kicks in — see below.
    from collections import deque
    queue: deque[list[dict]] = deque(_plan_batches(pages))
    initial_batches = len(queue)
    all_measurements: list[dict] = []
    header: dict = {}
    batch_num = 0
    near_cap_threshold = int(config.LLM_MAX_TOKENS * 0.85)

    def run_batch(batch: list[dict], label: str) -> tuple[dict, int]:
        try:
            message = _call_once(client, model_id, tool, batch, pdf_path)
        except Exception as e:
            raise LLMExtractionError(f"API call failed: {e}") from e
        _, truncated, empty, tool_input, out_tokens = _summarize_response(batch, message, label)
        # Retry once with a smaller batch if we truncated to nothing.
        if truncated and empty and len(batch) > 1:
            mid = len(batch) // 2
            print(f"    [llm]{label} retrying as 2 sub-batches of {mid} and {len(batch)-mid} pages", flush=True)
            left_input, left_out = run_batch(batch[:mid], label + ".a")
            right_input, right_out = run_batch(batch[mid:], label + ".b")
            merged = dict(left_input)
            merged["measurements"] = list(left_input.get("measurements") or []) + list(right_input.get("measurements") or [])
            for k in ("school_name", "district", "lab_name"):
                if not merged.get(k) and right_input.get(k):
                    merged[k] = right_input[k]
            return merged, max(left_out, right_out)
        return tool_input, out_tokens

    # Parallel to `all_measurements`: the 1-indexed page range (lo, hi) of the
    # batch that produced each row. The model doesn't return per-row page
    # numbers, so this is the best per-row citation we can attach — a
    # reviewer can jump to that page range in the source PDF and find the
    # row's source_text. Populated below and returned via the report object.
    all_page_ranges: list[tuple[int, int]] = []

    while queue:
        batch = queue.popleft()
        batch_num += 1
        label = f" [batch {batch_num}]" if initial_batches > 1 or batch_num > 1 else ""
        result, out_tokens = run_batch(batch, label)
        for k in ("school_name", "district", "lab_name"):
            if not header.get(k) and result.get(k):
                header[k] = result[k]
        rows = result.get("measurements") or []
        all_measurements.extend(rows)
        if batch:
            page_lo = min(p["page_num"] for p in batch) + 1
            page_hi = max(p["page_num"] for p in batch) + 1
            all_page_ranges.extend([(page_lo, page_hi)] * len(rows))
        else:
            all_page_ranges.extend([(0, 0)] * len(rows))

        # Adaptive shrink: if this batch nearly hit the output cap AND there
        # are remaining pages queued, split the next batch in half so the
        # next call has more headroom. Prevents a report where one batch
        # comes back at ~90% and the next silently truncates.
        if out_tokens >= near_cap_threshold and queue:
            nxt = queue.popleft()
            if len(nxt) > 1:
                mid = len(nxt) // 2
                queue.appendleft(nxt[mid:])
                queue.appendleft(nxt[:mid])
                print(
                    f"    [llm]{label} out={out_tokens} near cap "
                    f"({near_cap_threshold}); shrinking next batch to {mid} pages",
                    flush=True,
                )
            else:
                queue.appendleft(nxt)

    if batch_num > 1:
        print(f"    [llm] merged {batch_num} batches -> {len(all_measurements)} total measurements", flush=True)

    merged_payload = {**header, "measurements": all_measurements}
    try:
        report = ExtractedReport.model_validate(merged_payload)
    except Exception as e:
        raise LLMExtractionError(f"Merged response did not match schema: {e}") from e

    # Attach each row's page-range as an ad-hoc attribute on the Pydantic
    # instance itself, using object.__setattr__ to bypass Pydantic's normal
    # attribute-assignment guard. The validation gate reorders/splits the
    # measurements list but preserves the row objects, so this survives
    # validation without needing a parallel index. The pipeline reads
    # measurement._source_page_hi to set Measurement.source_page.
    if len(all_page_ranges) == len(report.measurements):
        for m, (lo, hi) in zip(report.measurements, all_page_ranges):
            object.__setattr__(m, "_source_page_lo", lo)
            object.__setattr__(m, "_source_page_hi", hi)
    return report
