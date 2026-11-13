"""Tests for the find-duplicates module."""

import os
from pathlib import Path

from njlead.duplicates import (
    find_all_duplicates,
    find_drive_duplicates,
    find_duplicates,
)


def _make_pdf(path: Path, content: bytes = b"%PDF-1.4 test content") -> None:
    """Create a minimal file that looks like a PDF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------
# Local-only tests (backward-compatible find_duplicates)
# ---------------------------------------------------------------------------

def test_exact_duplicates(tmp_path):
    """Files with identical content should be grouped as exact duplicates."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    content = b"%PDF-1.4 identical content here"
    _make_pdf(input_dir / "report_a.pdf", content)
    _make_pdf(input_dir / "subfolder" / "report_b.pdf", content)
    _make_pdf(input_dir / "unique.pdf", b"%PDF-1.4 different content")

    result = find_duplicates(input_dir, output_dir)

    assert result["exact_groups"] == 1
    assert result["exact_excluded"] == 1
    assert (output_dir / "duplicates_report.csv").exists()


def test_no_duplicates(tmp_path):
    """All unique files should produce zero groups."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _make_pdf(input_dir / "a.pdf", b"%PDF-1.4 aaa")
    _make_pdf(input_dir / "b.pdf", b"%PDF-1.4 bbb")

    result = find_duplicates(input_dir, output_dir)

    assert result["exact_groups"] == 0
    assert result["exact_excluded"] == 0


def test_exclude_keeps_shortest_path(tmp_path):
    """The exclude list should keep the file with the shortest path."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    content = b"%PDF-1.4 duplicate data"
    _make_pdf(input_dir / "short.pdf", content)
    _make_pdf(input_dir / "very" / "long" / "path" / "long_name.pdf", content)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        find_duplicates(input_dir, output_dir)
        exclude = (tmp_path / "exclude.txt").read_text()
        assert "short.pdf" not in exclude
        assert "long_name.pdf" in exclude
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Drive exact duplicate tests (mocked — no real Drive calls)
# ---------------------------------------------------------------------------

def test_drive_exact_groups_by_md5():
    """Files with the same md5Checksum on Drive should form duplicate groups."""
    drive_files = [
        {"id": "1", "name": "report.pdf", "relative_path": "report.pdf",
         "md5Checksum": "aaa111", "size": "5000"},
        {"id": "2", "name": "report_copy.pdf", "relative_path": "county/report_copy.pdf",
         "md5Checksum": "aaa111", "size": "5000"},
        {"id": "3", "name": "unique.pdf", "relative_path": "unique.pdf",
         "md5Checksum": "bbb222", "size": "3000"},
    ]

    groups, exclude = find_drive_duplicates(drive_files)

    assert len(groups) == 1
    assert len(exclude) == 1
    # Shorter path kept, longer excluded
    assert "county/report_copy.pdf" in exclude


def test_drive_no_duplicates():
    """All unique checksums should produce zero groups."""
    drive_files = [
        {"id": "1", "name": "a.pdf", "relative_path": "a.pdf",
         "md5Checksum": "aaa", "size": "100"},
        {"id": "2", "name": "b.pdf", "relative_path": "b.pdf",
         "md5Checksum": "bbb", "size": "200"},
    ]

    groups, exclude = find_drive_duplicates(drive_files)

    assert len(groups) == 0
    assert len(exclude) == 0


def test_drive_three_way_duplicate():
    """Three files with the same checksum should exclude two, keep one."""
    drive_files = [
        {"id": "1", "name": "a.pdf", "relative_path": "a.pdf",
         "md5Checksum": "same", "size": "100"},
        {"id": "2", "name": "b.pdf", "relative_path": "sub/b.pdf",
         "md5Checksum": "same", "size": "100"},
        {"id": "3", "name": "c.pdf", "relative_path": "sub/deep/c.pdf",
         "md5Checksum": "same", "size": "100"},
    ]

    groups, exclude = find_drive_duplicates(drive_files)

    assert len(groups) == 1
    assert len(exclude) == 2
    # Shortest path (a.pdf) should NOT be excluded
    assert "a.pdf" not in exclude


# ---------------------------------------------------------------------------
# Full workflow test (Drive + local)
# ---------------------------------------------------------------------------

def test_find_all_duplicates_combines_drive_and_local(tmp_path):
    """find_all_duplicates merges Drive and local results into one report."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"

    # Drive files: one duplicate group
    drive_files = [
        {"id": "1", "name": "orig.pdf", "relative_path": "orig.pdf",
         "md5Checksum": "xxx", "size": "100"},
        {"id": "2", "name": "copy.pdf", "relative_path": "sub/copy.pdf",
         "md5Checksum": "xxx", "size": "100"},
        {"id": "3", "name": "unique_drive.pdf", "relative_path": "unique_drive.pdf",
         "md5Checksum": "yyy", "size": "200"},
    ]

    # Local files: one separate duplicate group (different content from Drive dups)
    content = b"%PDF-1.4 local dup content"
    _make_pdf(input_dir / "local_a.pdf", content)
    _make_pdf(input_dir / "local_b.pdf", content)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = find_all_duplicates(drive_files, input_dir, output_dir)
    finally:
        os.chdir(old_cwd)

    assert result["drive_exact_groups"] == 1
    assert result["drive_exact_excluded"] == 1
    assert result["exact_groups"] == 1
    assert (output_dir / "duplicates_report.csv").exists()
