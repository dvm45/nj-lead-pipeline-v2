"""
CSV exporter — the engine behind `njlead export`.

Joins the database tables (measurements -> samples -> schools -> documents)
and enriches each row with school metadata from the NCES reference lookup.

Output columns (gold standard, from Trenton example):
  county, district, school_name, address, coordinates,
  sample_id, sample_location, fixture_type,
  sample_date, lead_concentration_ppb

Side effects:
  - Writes export_<timestamp>.csv to the output dir.
  - Writes data/reference/unmatched_schools.csv listing schools the matcher
    couldn't resolve, so they can be reviewed manually.

Behavior on missing data:
  - If the NCES lookup file isn't present, the export aborts with a message
    pointing the user to `njlead refresh-reference`.
  - If a specific school can't be matched, the row still ships — county,
    address, and coordinates are blank. The school is appended to
    unmatched_schools.csv.
"""

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import joinedload

from njlead.db.models import Document, Measurement, Sample, School
from njlead.db.session import get_session
from njlead.reference import LATEST_REFERENCE_YEAR
from njlead.reference.matcher import SchoolMatch, SchoolMatcher


# Gold-standard output columns, in order
CSV_COLUMNS = [
    "county",
    "district",
    "school_name",
    "address",
    "coordinates",
    "sample_id",
    "sample_location",
    "fixture_type",
    "sample_date",
    "lead_concentration_ppb",
]


def _format_coordinates(match: SchoolMatch | None) -> str:
    """
    Format lat/lng as 'LAT, LNG' to match the gold-standard CSV style.

    Returns "" when either coordinate is missing.
    """
    if match is None or match.latitude is None or match.longitude is None:
        return ""
    return f"{match.latitude}, {match.longitude}"


def _resolve_lookup_path() -> Path:
    """
    Return the expected path to the NCES lookup CSV. Doesn't check existence.
    """
    return Path.cwd() / "data" / "reference" / f"nj_schools_{LATEST_REFERENCE_YEAR}.csv"


def _write_unmatched_log(
    unmatched: dict[tuple[str, str], dict],
    dest_path: Path,
) -> None:
    """
    Write the running tally of schools that didn't clear the matcher threshold.

    Columns:
      school_name, district, occurrence_count, sample_file_examples
    """
    if not unmatched:
        # Nothing to report — remove any stale file from a previous export
        dest_path.unlink(missing_ok=True)
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["school_name", "district", "occurrence_count", "sample_file_examples"])
        # Sort by frequency so reviewers see the highest-impact gaps first
        rows = sorted(unmatched.items(), key=lambda kv: kv[1]["count"], reverse=True)
        for (school, district), info in rows:
            # Limit to first 5 example files to keep the cell readable
            examples = "; ".join(sorted(info["files"])[:5])
            writer.writerow([school, district, info["count"], examples])


def export_to_csv(output_dir: Path | None = None) -> Path:
    """
    Query the database and write all measurements to a CSV file.

    Arguments:
      output_dir: where to write the CSV (defaults to current directory)

    Returns:
      The path to the CSV file that was written.

    Raises:
      RuntimeError if no measurements are found, or if the NCES lookup
      file is missing (with a hint to run `njlead refresh-reference`).
    """
    if output_dir is None:
        output_dir = Path.cwd()

    # --- Step 1: confirm the reference data is available ----------------
    lookup_path = _resolve_lookup_path()
    if not lookup_path.exists():
        raise RuntimeError(
            f"Reference data not found at {lookup_path}.\n"
            "Run 'njlead refresh-reference' first to download NCES school metadata."
        )
    matcher = SchoolMatcher(lookup_path)

    # --- Step 2: prepare output paths -----------------------------------
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_path = output_dir / f"export_{timestamp}.csv"
    unmatched_path = Path.cwd() / "data" / "reference" / "unmatched_schools.csv"

    # Track unmatched (school, district) pairs to write to the side log.
    # Each entry holds: { 'count': N, 'files': set[str] }
    unmatched: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "files": set()})

    # --- Step 3: query and write ---------------------------------------
    with get_session() as session:
        measurements = (
            session.query(Measurement)
            .join(Sample, Measurement.sample_id == Sample.id)
            .outerjoin(School, Sample.school_id == School.id)
            .join(Document, Sample.document_id == Document.id)
            .options(
                joinedload(Measurement.sample).joinedload(Sample.school),
                joinedload(Measurement.sample).joinedload(Sample.document),
            )
            .order_by(Document.file_name, Sample.sample_id)
            .all()
        )

        if not measurements:
            raise RuntimeError(
                "No measurements found in the database.\n"
                "Run 'njlead ingest <folder>' first."
            )

        with open(output_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
            writer.writeheader()

            for m in measurements:
                sample = m.sample
                school = sample.school
                doc = sample.document

                # Match the parsed school to its NCES record. If we never
                # parsed a school name from the PDF, we can't even try.
                match: SchoolMatch | None = None
                if school is not None:
                    match = matcher.match(school.name, school.district)
                    if match is None:
                        key = (school.name, school.district or "")
                        unmatched[key]["count"] += 1
                        unmatched[key]["files"].add(doc.file_name)

                # Use the matched canonical district when possible, else the
                # one parsed from the PDF (so blanks don't propagate).
                district_out = (match.district_name if match else (school.district if school else "")) or ""
                school_name_out = (match.school_name if match else (school.name if school else "")) or ""

                writer.writerow({
                    "county": match.county if match else "",
                    "district": district_out,
                    "school_name": school_name_out,
                    "address": match.address if match else "",
                    "coordinates": _format_coordinates(match),
                    "sample_id": sample.sample_id or "",
                    "sample_location": sample.location or "",
                    "fixture_type": sample.fixture_type or "",
                    "sample_date": sample.sample_date.isoformat() if sample.sample_date else "",
                    "lead_concentration_ppb": m.result_ppb,
                })

    # --- Step 4: write the unmatched log -------------------------------
    _write_unmatched_log(unmatched, unmatched_path)

    return output_path
