"""
Validation gate for LLM-extracted measurements.

The model's output is never trusted directly. Every ExtractedMeasurement passes
through `validate_measurement()` BEFORE anything is written to SQLite. The gate
returns a decision - ACCEPT or REVIEW - plus reasons, so a held-back row becomes
an `issues` entry a human can look at instead of silent bad data.

Five checks, cheapest first:
  1. STRUCTURE  - required fields present and usable (Pydantic already did types;
     we re-assert the couple it can't express).
  2. RANGE      - physically plausible lead-in-water value.
  3. UNIT       - recognized unit; mg/L and ppm converted; unknown rejected;
     the model's result_ppb must match what we independently derive from raw.
  4. SOURCE-SPAN- the printed value must actually appear on the page
     (anti-hallucination).
  5. CONFIDENCE - per-row score at or above the configured threshold.

Any single failure routes the row to REVIEW.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from njlead.ingest.schema import ExtractedMeasurement

try:
    from njlead.config import CONFIDENCE_THRESHOLD
except Exception:  # config import should never fail, but stay defensive
    CONFIDENCE_THRESHOLD = 0.70


# --- tunable bounds (all in one place) -------------------------------------
RESULT_PPB_MIN = 0.0
RESULT_PPB_MAX = 50_000.0  # 5-digit ppb almost always = a misread zip/ID/phone

_UNIT_PPB_EQUIVALENT = {"ug/l", "µg/l", "ppb", "ppb (ug/l)", "ug/l (ppb)"}
_UNIT_NEEDS_CONVERSION = {"mg/l": 1000.0, "ppm": 1000.0}

_NON_DETECT_RE = re.compile(
    r"(?:^|\b)(?:ND|BDL|non[-\s]?detect(?:ed)?|not\s+detected|none\s+detected|<\s*(?:MDL|RL|LOQ))\b",
    re.IGNORECASE,
)
_LESS_THAN_RE = re.compile(r"<\s*(\d+(?:\.\d+)?)")
_PLAIN_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_PPB_ABS_TOL = 0.51  # absorbs rounding between printed value and result_ppb


class Decision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"


@dataclass
class ValidationResult:
    decision: Decision
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.decision is Decision.ACCEPT


def _normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("µ", "u").replace("μ", "u")  # micro signs -> 'u'
    s = s.lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _derive_ppb_from_raw(result_raw: str, unit_raw: str | None) -> float | None:
    """Independently re-compute ppb from the verbatim text, the way a human would."""
    raw = result_raw.strip()
    if _NON_DETECT_RE.search(raw):
        return 0.0
    value: float | None = None
    lt = _LESS_THAN_RE.search(raw)
    if lt:
        value = float(lt.group(1))
    else:
        num = _PLAIN_NUM_RE.search(raw)
        if num:
            value = float(num.group(1))
    if value is None:
        return None
    if unit_raw:
        u = _normalize_text(unit_raw)
        if u in _UNIT_NEEDS_CONVERSION:
            value *= _UNIT_NEEDS_CONVERSION[u]
    return value


def _check_structure(m: ExtractedMeasurement) -> list[str]:
    problems: list[str] = []
    if not m.sample_id and not m.sample_location:
        problems.append("row has neither sample_id nor sample_location")
    if not m.source_text or len(m.source_text.strip()) < 3:
        problems.append("missing/too-short source_text (cannot verify provenance)")
    return problems


def _check_range_and_unit(m: ExtractedMeasurement) -> list[str]:
    problems: list[str] = []
    if m.result_ppb < RESULT_PPB_MIN:
        problems.append(f"result_ppb {m.result_ppb} is negative")
    elif m.result_ppb > RESULT_PPB_MAX:
        problems.append(f"result_ppb {m.result_ppb} exceeds sane max {RESULT_PPB_MAX:.0f} (likely a misread)")
    if m.action_level_ppb is not None and not (1.0 <= m.action_level_ppb <= 100.0):
        problems.append(f"action_level_ppb {m.action_level_ppb} out of expected 1-100 range")
    if m.unit_raw is not None:
        u = _normalize_text(m.unit_raw)
        if u and u not in _UNIT_PPB_EQUIVALENT and u not in _UNIT_NEEDS_CONVERSION:
            problems.append(f"unrecognized unit '{m.unit_raw}'")
    derived = _derive_ppb_from_raw(m.result_raw, m.unit_raw)
    if derived is None:
        problems.append(f"could not derive a number from result_raw '{m.result_raw}'")
    elif abs(derived - m.result_ppb) > _PPB_ABS_TOL:
        problems.append(f"result_ppb {m.result_ppb} disagrees with value derived from raw '{m.result_raw}' (= {derived})")
    return problems


def _check_source_span(m: ExtractedMeasurement, page_text: str) -> list[str]:
    problems: list[str] = []
    norm_page = _normalize_text(page_text)
    norm_span = _normalize_text(m.source_text)
    norm_raw = _normalize_text(m.result_raw)
    if norm_span and norm_span not in norm_page:
        problems.append("source_text not found in report text (possible hallucination)")
    if norm_raw and norm_raw not in norm_span and norm_raw not in norm_page:
        problems.append(f"result_raw '{m.result_raw}' not found in cited source")
    return problems


def _check_confidence(m: ExtractedMeasurement) -> list[str]:
    if m.confidence < CONFIDENCE_THRESHOLD:
        return [f"confidence {m.confidence:.2f} below threshold {CONFIDENCE_THRESHOLD:.2f}"]
    return []


def validate_measurement(m: ExtractedMeasurement, page_text: str, check_source_span: bool = True) -> ValidationResult:
    """
    Gate one extracted measurement before it is written to SQLite.

    `page_text` is the report's raw text (all pages concatenated is fine); the
    source-span check verifies the reported value against it.

    `check_source_span` should be False for image-only (scanned) reports, where
    there is no extracted text to verify against - the model read the page as an
    image, so we rely on the range, unit and confidence checks instead.
    """
    reasons: list[str] = []
    reasons += _check_structure(m)
    reasons += _check_range_and_unit(m)
    if check_source_span:
        reasons += _check_source_span(m, page_text)
    reasons += _check_confidence(m)
    if reasons:
        return ValidationResult(decision=Decision.REVIEW, reasons=reasons)
    return ValidationResult(decision=Decision.ACCEPT)


def validate_report(
    measurements: list[ExtractedMeasurement], page_text: str, check_source_span: bool = True
) -> tuple[list[ExtractedMeasurement], list[tuple[ExtractedMeasurement, list[str]]]]:
    """Split a report's measurements into (accepted, held) where held = (row, reasons)."""
    accepted: list[ExtractedMeasurement] = []
    held: list[tuple[ExtractedMeasurement, list[str]]] = []
    for m in measurements:
        result = validate_measurement(m, page_text, check_source_span=check_source_span)
        if result.ok:
            accepted.append(m)
        else:
            held.append((m, result.reasons))
    return accepted, held
