"""Tests for the export module — columns, order, filters, fallbacks."""


from njlead.export.writer import CSV_COLUMNS

# --- Column checks ---

REQUIRED_COLUMNS = [
    "county", "district", "school_name", "address", "coordinates",
    "sample_id", "sample_location", "fixture_type", "draw_type",
    "sample_date", "year", "report_year", "year_source",
    "lead_concentration_ppb", "confidence", "source_page", "source_file",
]


def test_required_columns_exist():
    for col in REQUIRED_COLUMNS:
        assert col in CSV_COLUMNS, f"Missing required column: {col}"


def test_column_order():
    assert CSV_COLUMNS == REQUIRED_COLUMNS


def test_column_count():
    assert len(CSV_COLUMNS) == 17


# --- Analyte normalization ---

def test_analyte_normalizes_pb():
    from njlead.ingest.schema import ExtractedMeasurement

    m = ExtractedMeasurement(
        sample_id="S-001",
        analyte="Pb",
        result_ppb=5.0,
        result_raw="5.0",
        source_text="test 5.0",
        confidence=0.9,
    )
    assert m.analyte == "lead"


def test_analyte_normalizes_lead_pb():
    from njlead.ingest.schema import ExtractedMeasurement

    m = ExtractedMeasurement(
        sample_id="S-001",
        analyte="lead (pb)",
        result_ppb=5.0,
        result_raw="5.0",
        source_text="test 5.0",
        confidence=0.9,
    )
    assert m.analyte == "lead"


def test_analyte_preserves_copper():
    from njlead.ingest.schema import ExtractedMeasurement

    m = ExtractedMeasurement(
        sample_id="S-001",
        analyte="copper",
        result_ppb=50.0,
        result_raw="50.0",
        source_text="test 50.0",
        confidence=0.9,
    )
    assert m.analyte == "copper"


# --- County from path ---

def test_county_from_path():
    from njlead.export.writer import _county_from_path

    assert _county_from_path("C:/data/Passaic County/report.pdf") == "Passaic"
    assert _county_from_path("C:\\data\\Atlantic County\\sub\\file.pdf") == "Atlantic"
    assert _county_from_path("C:/data/report.pdf") == ""


# --- Dedup keys ---

def test_dedup_keys_defined():
    from njlead.export.writer import _DEDUP_KEYS
    assert "school_name" in _DEDUP_KEYS
    assert "sample_id" in _DEDUP_KEYS
    assert "lead_concentration_ppb" in _DEDUP_KEYS
