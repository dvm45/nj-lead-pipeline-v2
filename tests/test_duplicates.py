"""Tests for the find-duplicates module."""

from pathlib import Path

from njlead.duplicates import find_duplicates


def _make_pdf(path: Path, content: bytes = b"%PDF-1.4 test content") -> None:
    """Create a minimal file that looks like a PDF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


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
    assert result["total_files"] == 3

    # Check report was written
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

    # Temporarily change cwd for exclude.txt writing
    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        find_duplicates(input_dir, output_dir)
        exclude = (tmp_path / "exclude.txt").read_text()
        # The longer path should be excluded, not the shorter one
        assert "short.pdf" not in exclude
        assert "long_name.pdf" in exclude
    finally:
        os.chdir(old_cwd)
