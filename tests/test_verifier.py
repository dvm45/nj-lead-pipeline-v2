"""Tests for verifier behavior — rows should never be dropped by confidence capping."""

from njlead.ingest.schema import ExtractedMeasurement, ExtractedReport
from njlead.ingest.verifier import apply_verification


def _make_report(measurements=None, school_name="Test School", district="Test District"):
    return ExtractedReport(
        school_name=school_name,
        district=district,
        lab_name="EMSL",
        measurements=measurements or [],
    )


def _make_measurement(confidence=0.95, sample_id="S-001"):
    return ExtractedMeasurement(
        sample_id=sample_id,
        sample_location="Kitchen Sink",
        analyte="lead",
        result_ppb=5.0,
        result_raw="5.0",
        source_text="Kitchen Sink 5.0 ug/L",
        confidence=confidence,
    )


def test_low_overall_confidence_does_not_downgrade_rows():
    """When verifier has low overall confidence, rows keep their original confidence."""
    m = _make_measurement(confidence=0.90)
    report = _make_report(measurements=[m])
    findings = {
        "values_correct": True,
        "is_multi_school": False,
        "school_attribution_correct": True,
        "school_name_correct": True,
        "district_correct": True,
        "missed_count": 0,
        "flagged_rows": [],
        "overall_confidence": 0.40,
    }
    updated_report, issues = apply_verification(report, findings)

    # Row should keep its original confidence
    assert updated_report.measurements[0].confidence == 0.90
    # But an issue should be logged
    assert any("overall confidence low" in i for i in issues)


def test_multi_school_misattribution_keeps_rows():
    """Multi-school attribution error should log issue but not cap confidence."""
    m = _make_measurement(confidence=0.85)
    report = _make_report(measurements=[m])
    findings = {
        "values_correct": True,
        "is_multi_school": True,
        "school_attribution_correct": False,
        "school_name_correct": True,
        "district_correct": True,
        "missed_count": 0,
        "flagged_rows": [],
        "overall_confidence": 0.90,
    }
    updated_report, issues = apply_verification(report, findings)

    # Row keeps original confidence
    assert updated_report.measurements[0].confidence == 0.85
    # Issue is logged
    assert any("attribution" in i for i in issues)


def test_flagged_rows_are_logged_not_downgraded():
    """Flagged rows should be logged but not have confidence changed."""
    m = _make_measurement(confidence=0.92, sample_id="FLAG-001")
    report = _make_report(measurements=[m])
    findings = {
        "values_correct": True,
        "is_multi_school": False,
        "school_attribution_correct": True,
        "school_name_correct": True,
        "district_correct": True,
        "missed_count": 0,
        "flagged_rows": [{"sample_id": "FLAG-001", "issue": "value mismatch"}],
        "overall_confidence": 0.90,
    }
    updated_report, issues = apply_verification(report, findings)

    # Confidence unchanged
    assert updated_report.measurements[0].confidence == 0.92
    # Flagged row is logged
    assert any("FLAG-001" in i for i in issues)


def test_school_name_correction():
    """Verifier can still correct school names."""
    report = _make_report(school_name="Wro ng Name")
    findings = {
        "values_correct": True,
        "is_multi_school": False,
        "school_attribution_correct": True,
        "school_name_correct": False,
        "school_name_suggested": "Correct Name",
        "district_correct": True,
        "missed_count": 0,
        "flagged_rows": [],
        "overall_confidence": 0.95,
    }
    updated_report, issues = apply_verification(report, findings)
    assert updated_report.school_name == "Correct Name"
