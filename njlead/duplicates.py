"""
Duplicate PDF detection — no Bedrock calls.

Four detection methods (in order):
  1. Drive exact: group by Drive md5Checksum (no download required)
  2. Local exact: MD5 hash match on downloaded files
  3. Text similarity: ≥90% text overlap via rapidfuzz (near-identical PDFs)
  4. DB overlap: ≥80% shared (sample_id, lead_value) rows in leads.db

Outputs:
  - output/duplicates_report.csv — all detected duplicate groups
  - exclude.txt — exact duplicates auto-excluded (keep shortest path)
"""

from __future__ import annotations

import csv
import hashlib
import logging
from pathlib import Path

_log = logging.getLogger(__name__)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_text(path: Path) -> str:
    """Extract all text from a PDF. Returns empty string on failure."""
    try:
        import fitz
        doc = fitz.open(str(path))
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
        return text
    except Exception:
        return ""


def _page_count(path: Path) -> int:
    try:
        import fitz
        doc = fitz.open(str(path))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Method 1: Drive exact duplicates (no download)
# ---------------------------------------------------------------------------

def find_drive_duplicates(drive_files: list[dict]) -> tuple[dict[str, list[dict]], list[str]]:
    """
    Group Drive files by md5Checksum to find exact duplicates.

    Args:
        drive_files: list of dicts from drive.list_drive_pdfs(), each with
                     keys: name, relative_path, md5Checksum, size, id

    Returns:
        (groups, exclude_paths) where groups maps md5 → list of file dicts,
        and exclude_paths is the list of relative_paths to exclude (keeps
        the shortest path in each group).
    """
    by_md5: dict[str, list[dict]] = {}
    for f in drive_files:
        md5 = f.get("md5Checksum", "")
        if md5:
            by_md5.setdefault(md5, []).append(f)

    dup_groups = {md5: files for md5, files in by_md5.items() if len(files) > 1}
    _log.info("Drive: %d exact duplicate groups (of %d files)",
              len(dup_groups), len(drive_files))

    exclude_paths: list[str] = []
    for files in dup_groups.values():
        sorted_files = sorted(files, key=lambda f: len(f.get("relative_path", f["name"])))
        for f in sorted_files[1:]:
            exclude_paths.append(f.get("relative_path", f["name"]))

    return dup_groups, exclude_paths


# ---------------------------------------------------------------------------
# Method 2+3: Local exact + text similarity (requires downloaded files)
# ---------------------------------------------------------------------------

def find_local_duplicates(
    input_dir: Path,
    skip_names: set[str] | None = None,
) -> tuple[dict[str, list[Path]], list[tuple[Path, Path, float, str]]]:
    """
    Scan local files for exact MD5 matches and text similarity.

    Args:
        input_dir: folder containing downloaded PDFs
        skip_names: filenames already flagged as Drive-level duplicates (skip them)

    Returns:
        (exact_groups, text_similar) — exact_groups maps md5→list[Path],
        text_similar is a list of (file_a, file_b, score, match_type).
    """
    if skip_names is None:
        skip_names = set()

    pdf_files = sorted(input_dir.rglob("*.pdf")) + sorted(input_dir.rglob("*.PDF"))
    seen_paths: set[Path] = set()
    unique_pdfs: list[Path] = []
    for p in pdf_files:
        resolved = p.resolve()
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            if p.name not in skip_names:
                unique_pdfs.append(p)

    _log.info("Scanning %d local PDFs for duplicates...", len(unique_pdfs))

    # Exact MD5 duplicates
    hash_groups: dict[str, list[Path]] = {}
    for p in unique_pdfs:
        h = _md5(p)
        hash_groups.setdefault(h, []).append(p)
    exact_groups = {h: paths for h, paths in hash_groups.items() if len(paths) > 1}
    _log.info("Local: %d exact duplicate groups", len(exact_groups))

    # Text similarity on remaining files
    texts: dict[Path, str] = {}
    page_counts: dict[Path, int] = {}
    for p in unique_pdfs:
        texts[p] = _extract_text(p)
        page_counts[p] = _page_count(p)

    exact_files: set[Path] = set()
    for paths in exact_groups.values():
        exact_files.update(paths)
    remaining = [p for p in unique_pdfs if p not in exact_files]

    from rapidfuzz import fuzz
    text_similar: list[tuple[Path, Path, float, str]] = []
    for i, a in enumerate(remaining):
        for b in remaining[i + 1:]:
            if abs(page_counts[a] - page_counts[b]) > 2:
                continue
            text_a, text_b = texts[a], texts[b]
            if text_a and text_b:
                score = fuzz.ratio(text_a[:5000], text_b[:5000])
                if score >= 90:
                    text_similar.append((a, b, score, "text_similar"))
            elif not text_a and not text_b and page_counts[a] == page_counts[b]:
                size_a, size_b = a.stat().st_size, b.stat().st_size
                size_ratio = min(size_a, size_b) / max(size_a, size_b) if max(size_a, size_b) > 0 else 0
                if size_ratio >= 0.90:
                    text_similar.append((a, b, size_ratio * 100, "possible_scan_dup"))

    _log.info("Local: %d text-similar pairs", len(text_similar))
    return exact_groups, text_similar


# ---------------------------------------------------------------------------
# Method 4: DB overlap
# ---------------------------------------------------------------------------

def find_db_duplicates(input_dir: Path) -> list[tuple[Path, Path, float]]:
    """Check DB for files sharing ≥80% of rows. Requires leads.db."""
    db_similar: list[tuple[Path, Path, float]] = []
    db_path = Path.cwd() / "leads.db"
    if not db_path.exists():
        return db_similar

    try:
        from njlead.db.models import Document, Measurement, Sample
        from njlead.db.session import get_session, init_db
        init_db(db_path)

        with get_session() as session:
            file_rows: dict[str, set[tuple]] = {}
            docs = session.query(Document).all()
            for doc in docs:
                samples = session.query(Sample).filter(Sample.document_id == doc.id).all()
                rows: set[tuple] = set()
                for s in samples:
                    meas = session.query(Measurement).filter(Measurement.sample_id == s.id).all()
                    for m in meas:
                        rows.add((s.sample_id, m.result_ppb))
                if rows:
                    file_rows[doc.file_name] = rows

            filenames = list(file_rows.keys())
            for i, a in enumerate(filenames):
                for b in filenames[i + 1:]:
                    rows_a, rows_b = file_rows[a], file_rows[b]
                    overlap = rows_a & rows_b
                    total = min(len(rows_a), len(rows_b))
                    if total > 0 and len(overlap) / total >= 0.80:
                        score = len(overlap) / total * 100
                        db_similar.append((input_dir / a, input_dir / b, score))

        _log.info("DB: %d overlap pairs", len(db_similar))
    except Exception as e:
        _log.warning("DB overlap check failed: %s", e)

    return db_similar


# ---------------------------------------------------------------------------
# Write report and exclude.txt
# ---------------------------------------------------------------------------

def _write_report(
    output_dir: Path,
    drive_groups: dict[str, list[dict]],
    local_groups: dict[str, list[Path]],
    text_similar: list[tuple[Path, Path, float, str]],
    db_similar: list[tuple[Path, Path, float]],
    input_dir: Path | None,
    drive_exclude: list[str],
) -> list[str]:
    """Write duplicates_report.csv and return all exclude paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "duplicates_report.csv"
    group_id = 0
    all_exclude: list[str] = list(drive_exclude)

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "group_id", "match_type", "score", "file_path", "size_bytes", "page_count",
        ])
        writer.writeheader()

        # Drive exact groups
        for _md5, files in sorted(drive_groups.items()):
            group_id += 1
            sorted_files = sorted(files, key=lambda x: len(x.get("relative_path", x["name"])))
            for file_dict in sorted_files:
                writer.writerow({
                    "group_id": group_id,
                    "match_type": "drive_exact",
                    "score": 100.0,
                    "file_path": file_dict.get("relative_path", file_dict["name"]),
                    "size_bytes": file_dict.get("size", 0),
                    "page_count": "",
                })

        # Local exact groups
        for _h, paths in sorted(local_groups.items()):
            group_id += 1
            paths_sorted = sorted(paths, key=lambda p: len(str(p)))
            for i, p in enumerate(paths_sorted):
                rel = str(p.relative_to(input_dir)) if input_dir and p.is_relative_to(input_dir) else str(p)
                writer.writerow({
                    "group_id": group_id,
                    "match_type": "local_exact",
                    "score": 100.0,
                    "file_path": rel,
                    "size_bytes": p.stat().st_size,
                    "page_count": _page_count(p),
                })
                if i > 0:
                    all_exclude.append(rel)

        # Text-similar pairs
        for a, b, score, match_type in text_similar:
            group_id += 1
            for p in (a, b):
                rel = str(p.relative_to(input_dir)) if input_dir and p.is_relative_to(input_dir) else str(p)
                writer.writerow({
                    "group_id": group_id,
                    "match_type": match_type,
                    "score": f"{score:.1f}",
                    "file_path": rel,
                    "size_bytes": p.stat().st_size,
                    "page_count": "",
                })

        # DB-overlap pairs
        for a, b, score in db_similar:
            group_id += 1
            for p in (a, b):
                rel = str(p.relative_to(input_dir)) if input_dir and p.is_relative_to(input_dir) else str(p)
                writer.writerow({
                    "group_id": group_id,
                    "match_type": "db_overlap",
                    "score": f"{score:.1f}",
                    "file_path": rel,
                    "size_bytes": p.stat().st_size if p.exists() else 0,
                    "page_count": "",
                })

    _log.info("Wrote %s", report_path)
    return all_exclude


def _write_exclude(exclude_paths: list[str]) -> None:
    """Write exclude.txt from a list of relative paths."""
    if not exclude_paths:
        return
    exclude_file = Path.cwd() / "exclude.txt"
    with open(exclude_file, "w", encoding="utf-8") as f:
        f.write("# Auto-generated by njlead find-duplicates\n")
        f.write("# Exact duplicates — the shorter-path copy is kept\n")
        for p in sorted(set(exclude_paths)):
            f.write(p + "\n")
    _log.info("Wrote %s (%d files to exclude)", exclude_file, len(set(exclude_paths)))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def find_duplicates(input_dir: Path, output_dir: Path) -> dict:
    """
    Local-only duplicate scan (backward-compatible entry point).

    For the full workflow including Drive, use find_all_duplicates().
    """
    local_groups, text_similar = find_local_duplicates(input_dir)
    db_similar = find_db_duplicates(input_dir)

    all_exclude = _write_report(
        output_dir, {}, local_groups, text_similar, db_similar, input_dir, [],
    )
    _write_exclude(all_exclude)

    return {
        "total_files": len(set(input_dir.rglob("*.pdf")) | set(input_dir.rglob("*.PDF"))),
        "drive_exact_groups": 0,
        "drive_exact_excluded": 0,
        "exact_groups": len(local_groups),
        "exact_excluded": sum(len(v) - 1 for v in local_groups.values()),
        "text_similar_pairs": len(text_similar),
        "db_overlap_pairs": len(db_similar),
    }


def find_all_duplicates(
    drive_files: list[dict],
    input_dir: Path | None,
    output_dir: Path,
) -> dict:
    """
    Full duplicate check: Drive exact → local exact + text sim → DB overlap.

    Args:
        drive_files: file listing from drive.list_drive_pdfs()
        input_dir: local folder with downloaded PDFs (None if not yet downloaded)
        output_dir: where to write the report CSV
    """
    # Step 1: Drive exact duplicates (no download needed)
    drive_groups, drive_exclude = find_drive_duplicates(drive_files)

    # Steps 2+3: Local checks (only if files exist locally)
    local_groups: dict[str, list[Path]] = {}
    text_similar: list[tuple[Path, Path, float, str]] = []
    if input_dir and input_dir.exists() and any(input_dir.rglob("*.pdf")):
        drive_excluded_names = {Path(p).name for p in drive_exclude}
        local_groups, text_similar = find_local_duplicates(input_dir, skip_names=drive_excluded_names)

    # Step 4: DB overlap
    db_similar: list[tuple[Path, Path, float]] = []
    if input_dir:
        db_similar = find_db_duplicates(input_dir)

    # Write report and exclude.txt
    all_exclude = _write_report(
        output_dir, drive_groups, local_groups, text_similar, db_similar,
        input_dir, drive_exclude,
    )
    _write_exclude(all_exclude)

    local_file_count = 0
    if input_dir and input_dir.exists():
        local_file_count = len(set(input_dir.rglob("*.pdf")) | set(input_dir.rglob("*.PDF")))

    return {
        "drive_files": len(drive_files),
        "drive_exact_groups": len(drive_groups),
        "drive_exact_excluded": len(drive_exclude),
        "local_files": local_file_count,
        "exact_groups": len(local_groups),
        "exact_excluded": sum(len(v) - 1 for v in local_groups.values()),
        "text_similar_pairs": len(text_similar),
        "db_overlap_pairs": len(db_similar),
    }
