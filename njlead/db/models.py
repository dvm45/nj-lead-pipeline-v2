"""
Database models for the NJ lead pipeline.

Each class here maps to one table in leads.db.
SQLAlchemy handles creating the tables and reading/writing rows —
you never have to write raw SQL.

Tables:
  documents    — one row per PDF file ingested
  pages        — one row per page per PDF, stores raw extracted text
  schools      — deduplicated school + district names
  samples      — one water sample per row (location, date, sample ID)
  measurements — lead ppb result for each sample
  issues       — anything that went wrong during ingest (parse failures, blank pages, etc.)
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# All models inherit from this base class.
# It tells SQLAlchemy that these classes represent database tables.
class Base(DeclarativeBase):
    pass


class Document(Base):
    """
    One row per PDF file.

    'status' tracks whether extraction went well:
      'ok'      — school, samples, and measurements were all found
      'partial' — some data was found but something was also logged to issues
      'failed'  — nothing useful was extracted
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The full path to the file on disk, e.g. C:/data/report_2019.pdf
    file_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    # Just the filename without the folder, e.g. report_2019.pdf
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)

    # SHA-256 fingerprint of the file contents — used to skip duplicates
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    page_count: Mapped[int] = mapped_column(Integer, nullable=True)

    # When this file was added to the database
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # 'ok', 'partial', or 'failed'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")

    # Related rows in other tables (SQLAlchemy loads these on demand)
    pages: Mapped[list["Page"]] = relationship("Page", back_populates="document")
    samples: Mapped[list["Sample"]] = relationship("Sample", back_populates="document")
    issues: Mapped[list["Issue"]] = relationship("Issue", back_populates="document")


class Page(Base):
    """
    One row per page per PDF.

    Stores the raw text that PyMuPDF extracted.
    If 'is_blank' is True, the page had no text — likely a scanned image
    that needs OCR (logged separately in the issues table).
    """

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id"), nullable=False
    )

    # 0-indexed page number (page 0 = first page)
    page_num: Mapped[int] = mapped_column(Integer, nullable=False)

    # The full text of this page as extracted by PyMuPDF
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # True if PyMuPDF returned an empty string for this page
    is_blank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    document: Mapped["Document"] = relationship("Document", back_populates="pages")


class School(Base):
    """
    One row per school (deduplicated by name + district).

    Multiple PDFs may reference the same school — they all point to
    the same row here rather than duplicating the name.

    The metadata fields (county, address, latitude, longitude, nces_id)
    are populated by joining to the NCES Common Core of Data reference
    file at export time. They stay null until that join succeeds.
    """

    __tablename__ = "schools"

    # Enforce that each name+district combination is stored only once
    __table_args__ = (UniqueConstraint("name", "district", name="uq_school_name_district"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    district: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Always 'NJ' for this pipeline — kept for future use
    state: Mapped[str] = mapped_column(String(8), nullable=False, default="NJ")

    # Metadata sourced from NCES CCD (filled in by the matcher; null until matched)
    county: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    nces_id: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Lab / vendor that produced the report (e.g. "EMSL", "LEW Environmental",
    # "RAMM"). Populated by the LLM engine from the report letterhead; the
    # regex engine leaves it null. Attached to the school so we can see which
    # labs test which districts without adding a new table.
    lab_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # rapidfuzz score (0–100) recorded for auditability — low scores warrant review
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 'matched', 'unmatched', or 'not_attempted'
    match_status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_attempted")

    samples: Mapped[list["Sample"]] = relationship("Sample", back_populates="school")


class Sample(Base):
    """
    One row per water sample.

    A sample is a single water draw at a specific location on a specific date.
    Each sample has one or more measurements (usually just one: lead ppb).
    """

    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id"), nullable=False
    )

    # May be null if we couldn't identify which school this report belongs to
    school_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("schools.id"), nullable=True
    )

    # The sample identifier as printed in the report (e.g. "S-001", "LCR-042")
    sample_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Where the sample was taken (e.g. "Boys Bathroom Sink 1F", "Kitchen Faucet")
    # This is the original, untouched string from the report — we never lose info.
    location: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The fixture portion of the location, split off by a keyword heuristic
    # (e.g. "Water Chiller", "Bottle Filler", "Sink Faucet"). May be null if
    # no known fixture keyword appeared in the location string.
    fixture_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Full date if available (e.g. 2019-05-14)
    sample_date: Mapped[date | None] = mapped_column(nullable=True)

    # Year only — used when we can find the year but not the full date
    test_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    document: Mapped["Document"] = relationship("Document", back_populates="samples")
    school: Mapped["School | None"] = relationship("School", back_populates="samples")
    measurements: Mapped[list["Measurement"]] = relationship(
        "Measurement", back_populates="sample"
    )


class Measurement(Base):
    """
    One row per lab result.

    Linked to a sample. For lead testing, 'analyte' is always 'lead'
    and 'result_ppb' is the measured concentration in parts per billion.

    'exceeds_action_level' is set to True when result_ppb > action_level_ppb.
    The EPA action level for lead is 15 ppb.
    """

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("samples.id"), nullable=False
    )

    # What was tested — default is 'lead', but some reports also test copper
    analyte: Mapped[str] = mapped_column(String(64), nullable=False, default="lead")

    # The measured concentration in parts per billion (ppb)
    result_ppb: Mapped[float] = mapped_column(Float, nullable=False)

    # The action level from the report (often 15.0 ppb)
    action_level_ppb: Mapped[float | None] = mapped_column(Float, nullable=True)

    # True if result_ppb > action_level_ppb (set automatically on insert)
    exceeds_action_level: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Citation fields (populated by the LLM engine) -------------------
    # A short verbatim snippet (~160 chars) the model quoted from the report
    # to prove this row exists. Used by the validation gate and preserved
    # here so a human reviewer can grep the source PDF and find the row.
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The model's own confidence in this row (0.0 - 1.0). Preserved so low-
    # confidence rows can be surfaced in review UIs even after ingest.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 1-indexed page number the row was extracted from. When a report is
    # batched, this is the last page of the batch (the model doesn't return
    # per-row page numbers). Nullable because pre-B rows have no value.
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)

    sample: Mapped["Sample"] = relationship("Sample", back_populates="measurements")


class Issue(Base):
    """
    One row per problem encountered during ingest.

    Instead of crashing when something goes wrong, the pipeline logs
    a row here and continues. You can review issues later with /inspect-issues.

    Common issue_type values:
      'ocr_needed'        — page was blank (scanned image, needs OCR)
      'no_school_found'   — couldn't identify school name
      'no_measurements'   — couldn't find any lead values on a page
      'extraction_failed' — unexpected error while processing a file
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id"), nullable=False
    )

    # Which page had the problem (None if the issue is file-level, not page-level)
    page_num: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Short category label — use the constants above for consistency
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Human-readable explanation of what went wrong
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    document: Mapped["Document"] = relationship("Document", back_populates="issues")
