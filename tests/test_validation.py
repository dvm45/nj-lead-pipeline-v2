"""Tests for the validation gate — all 5 checks, pass and fail cases."""


from njlead.ingest.schema import ExtractedMeasurement
from njlead.ingest.validation import (
    validate_measurement,
    validate_report,
)


def _make_measurement(**overrides) -> ExtractedMeasurement:
    """Build a valid measurement with sensible defaults, overriding as needed."""
    defaults = {
        "sample_id": "S-001",
        "sample_location": "Kitchen Sink",
        "fixture_type": "Sink",
        "analyte": "lead",
        "result_ppb": 5.0,
        "result_raw": "5.0",
        "unit_raw": "ug/L",
        "source_text": "Kitchen Sink 5.0 ug/L",
        "confidence": 0.95,
    }
    defaults.update(overrides)
    return ExtractedMeasurement(**defaults)


# --- Check 1: STRUCTURE ---

def test_structure_pass():
    m = _make_measurement()
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert result.ok


def test_structure_no_id_no_location():
    m = _make_measurement(sample_id=None, sample_location=None)
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert not result.ok
    assert any("neither sample_id nor sample_location" in r for r in result.reasons)


def test_structure_missing_source_text():
    m = _make_measurement(source_text="")
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert not result.ok
    assert any("source_text" in r for r in result.reasons)


# --- Check 2: RANGE ---

def test_range_pass_normal():
    m = _make_measurement(result_ppb=15.0, result_raw="15.0")
    result = validate_measurement(m, "Kitchen Sink 15.0 ug/L")
    assert result.ok


def test_range_pass_non_detect():
    m = _make_measurement(result_ppb=0.0, result_raw="ND")
    result = validate_measurement(m, "Kitchen Sink ND")
    assert result.ok


def test_range_fail_negative():
    m = _make_measurement(result_ppb=-1.0, result_raw="-1.0")
    result = validate_measurement(m, "Kitchen Sink -1.0 ug/L")
    assert not result.ok
    assert any("negative" in r for r in result.reasons)


def test_range_fail_too_high():
    m = _make_measurement(result_ppb=99999.0, result_raw="99999")
    result = validate_measurement(m, "Kitchen Sink 99999 ug/L")
    assert not result.ok
    assert any("sane max" in r for r in result.reasons)


# --- Check 3: UNIT ---

def test_unit_pass_ppb():
    m = _make_measurement(unit_raw="ppb")
    result = validate_measurement(m, "Kitchen Sink 5.0 ppb")
    assert result.ok


def test_unit_pass_mg_l_conversion():
    m = _make_measurement(result_ppb=5000.0, result_raw="5.0", unit_raw="mg/L")
    result = validate_measurement(m, "Kitchen Sink 5.0 mg/L")
    assert result.ok


def test_unit_fail_unrecognized():
    m = _make_measurement(unit_raw="gallons")
    result = validate_measurement(m, "Kitchen Sink 5.0 gallons")
    assert not result.ok
    assert any("unrecognized unit" in r for r in result.reasons)


def test_unit_ppb_mismatch():
    m = _make_measurement(result_ppb=10.0, result_raw="5.0", unit_raw="ug/L")
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert not result.ok
    assert any("disagrees" in r for r in result.reasons)


# --- Check 4: SOURCE-SPAN ---

def test_source_span_pass():
    text = "Sample S-001 Kitchen Sink: 5.0 ug/L"
    m = _make_measurement()
    result = validate_measurement(m, text, check_source_span=True)
    assert result.ok


def test_source_span_fail_value_not_in_text():
    text = "Some completely different text with no numbers"
    m = _make_measurement()
    result = validate_measurement(m, text, check_source_span=True)
    assert not result.ok
    assert any("not found" in r for r in result.reasons)


def test_source_span_skip_for_images():
    text = ""
    m = _make_measurement()
    result = validate_measurement(m, text, check_source_span=False)
    assert result.ok


def test_source_span_location_hallucination():
    text = "Hallway Fountain: 5.0 ug/L"
    m = _make_measurement(sample_location="Blueberry Wing Fountain")
    result = validate_measurement(m, text, check_source_span=True)
    assert not result.ok
    assert any("hallucination" in r for r in result.reasons)


# --- Check 5: CONFIDENCE ---

def test_confidence_pass():
    m = _make_measurement(confidence=0.90)
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert result.ok


def test_confidence_fail_below_threshold():
    m = _make_measurement(confidence=0.50)
    result = validate_measurement(m, "Kitchen Sink 5.0 ug/L")
    assert not result.ok
    assert any("confidence" in r for r in result.reasons)


# --- validate_report splits accepted/held ---

def test_validate_report_splits():
    good = _make_measurement(confidence=0.95)
    bad = _make_measurement(confidence=0.30, sample_id="BAD-001")
    text = "Kitchen Sink 5.0 ug/L"

    accepted, held = validate_report([good, bad], text)
    assert len(accepted) == 1
    assert len(held) == 1
    assert held[0][0].sample_id == "BAD-001"
