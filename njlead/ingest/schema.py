"""
Unified extraction schema for the LLM engine.

This is the "blank form" we hand to the model. The model fills it in; it never
gets to change its shape. That is the schema-stays-ours principle: every field
is defined here, in plain Pydantic, in our repo.

These models map onto the existing database tables with almost no translation
(see the field notes below and the writer in llm_pipeline.py).

Two design decisions are baked in:
  1. Lab identity is a FIELD (lab_name), not a routing decision. This is what
     lets one call replace the nine format-specific regex parsers.
  2. Every measurement carries its own evidence (result_raw, unit_raw,
     source_text) and a confidence score, so the validation gate can re-check
     the value and route doubtful rows to human review. None of these reach the
     final CSV - they exist only to be verified.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


class ExtractedMeasurement(BaseModel):
    """
    One water sample and its lead result, as returned by the model.

    Maps to the existing tables like this:
        sample_id        -> Sample.sample_id
        sample_location  -> Sample.location      (note the rename)
        fixture_type     -> Sample.fixture_type
        sample_date      -> Sample.sample_date
        test_year        -> Sample.test_year
        analyte          -> Measurement.analyte
        result_ppb       -> Measurement.result_ppb
        action_level_ppb -> Measurement.action_level_ppb
    result_raw, unit_raw, source_text and confidence are NOT stored as data -
    they feed the validation gate and can be logged to `issues` on review.
    """

    sample_id: str | None = Field(
        default=None,
        description="Sample identifier exactly as printed (e.g. 'AD19416-01', '24-0909-04'). Null if none.",
    )
    sample_location: str | None = Field(
        default=None,
        description="Where the sample was drawn, place portion only (e.g. '1st Floor Teachers Lounge').",
    )
    fixture_type: str | None = Field(
        default=None,
        description="The fixture, separated from the location (e.g. 'Water Chiller Fountain', 'Bottle Filler', 'Sink'). Null if none named.",
    )

    analyte: str = Field(default="lead", description="What was measured; almost always 'lead'.")
    result_ppb: float = Field(
        ...,
        description="Lead concentration normalized to ppb (= ug/L). 'ND'/'None Detected' -> 0.0. '<1.00' -> the detection limit (1.0).",
    )
    result_raw: str = Field(
        ...,
        description="The result EXACTLY as printed before normalization (e.g. 'ND', '<1.00', '5.74').",
    )
    unit_raw: str | None = Field(
        default=None,
        description="The unit exactly as printed next to the result (e.g. 'ug/L', 'ppb', 'mg/L').",
    )
    action_level_ppb: float | None = Field(
        default=15.0, description="Action level threshold from the report, usually 15 ppb."
    )

    sample_date: date | None = Field(
        default=None, description="Full collection date if present (ISO 'YYYY-MM-DD')."
    )
    test_year: int | None = Field(
        default=None, description="Year only, used when the full date isn't available."
    )

    source_text: str = Field(
        ...,
        description="The verbatim line(s) from the report this row was read from. Used to verify the value really appears in the source.",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Model's confidence in THIS row, 0-1."
    )

    @field_validator("analyte")
    @classmethod
    def _normalize_analyte(cls, v: str) -> str:
        return v.strip().lower() if v else "lead"


class ExtractedReport(BaseModel):
    """
    Everything the model pulls from one report (one PDF).

    Maps to:
        school_name -> School.name
        district    -> School.district
        lab_name    -> School.lab_name  (new nullable column)
    """

    school_name: str | None = Field(
        default=None, description="School / facility name (e.g. 'Cadwalader Elementary School')."
    )
    district: str | None = Field(default=None, description="District name (e.g. 'Trenton').")
    lab_name: str | None = Field(
        default=None,
        description="The lab / vendor that produced the report (e.g. 'EMSL', 'LEW Environmental', 'RAMM'). Read off the letterhead; null if unclear.",
    )
    measurements: list[ExtractedMeasurement] = Field(
        default_factory=list,
        description="Every water-sample lead result found. An empty list is valid (cover pages, narrative pages).",
    )
