"""Tests for the master.csv merge logic in runner.py."""

import csv
from pathlib import Path

from njlead.runner import _merge_csv


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


FIELDS = ["school_name", "lead_concentration_ppb", "source_file"]


def test_merge_new_rows_append(tmp_path):
    existing = tmp_path / "existing.csv"
    _write_csv(existing, [
        {"school_name": "School A", "lead_concentration_ppb": "5.0", "source_file": "a.pdf"},
    ], FIELDS)

    new = tmp_path / "new.csv"
    _write_csv(new, [
        {"school_name": "School B", "lead_concentration_ppb": "10.0", "source_file": "b.pdf"},
    ], FIELDS)

    output = tmp_path / "merged.csv"
    total = _merge_csv(
        existing, new, output,
        key_column="source_file",
        source_files_this_run={"b.pdf"},
        full_refresh=False,
    )
    assert total == 2

    with open(output, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    names = [r["school_name"] for r in rows]
    assert "School A" in names
    assert "School B" in names


def test_merge_reprocessed_replaces(tmp_path):
    existing = tmp_path / "existing.csv"
    _write_csv(existing, [
        {"school_name": "School A", "lead_concentration_ppb": "5.0", "source_file": "a.pdf"},
        {"school_name": "School A", "lead_concentration_ppb": "7.0", "source_file": "a.pdf"},
    ], FIELDS)

    new = tmp_path / "new.csv"
    _write_csv(new, [
        {"school_name": "School A", "lead_concentration_ppb": "6.0", "source_file": "a.pdf"},
    ], FIELDS)

    output = tmp_path / "merged.csv"
    total = _merge_csv(
        existing, new, output,
        key_column="source_file",
        source_files_this_run={"a.pdf"},
        full_refresh=False,
    )
    # Old 2 rows from a.pdf should be replaced by the 1 new row
    assert total == 1

    with open(output, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["lead_concentration_ppb"] == "6.0"


def test_full_refresh_ignores_existing(tmp_path):
    existing = tmp_path / "existing.csv"
    _write_csv(existing, [
        {"school_name": "Old School", "lead_concentration_ppb": "99.0", "source_file": "old.pdf"},
    ], FIELDS)

    new = tmp_path / "new.csv"
    _write_csv(new, [
        {"school_name": "New School", "lead_concentration_ppb": "1.0", "source_file": "new.pdf"},
    ], FIELDS)

    output = tmp_path / "merged.csv"
    total = _merge_csv(
        existing, new, output,
        key_column="source_file",
        source_files_this_run={"new.pdf"},
        full_refresh=True,
    )
    assert total == 1

    with open(output, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["school_name"] == "New School"


def test_merge_no_existing(tmp_path):
    new = tmp_path / "new.csv"
    _write_csv(new, [
        {"school_name": "Solo School", "lead_concentration_ppb": "3.0", "source_file": "solo.pdf"},
    ], FIELDS)

    output = tmp_path / "merged.csv"
    total = _merge_csv(
        None, new, output,
        key_column="source_file",
        source_files_this_run={"solo.pdf"},
        full_refresh=False,
    )
    assert total == 1
