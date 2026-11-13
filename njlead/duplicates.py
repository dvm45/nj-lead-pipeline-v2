"""
Duplicate PDF detection — no Bedrock calls.

Three detection methods:
  1. Exact: MD5 hash match (identical files with different names)
  2. Text similarity: ≥90% text overlap via rapidfuzz (near-identical PDFs)
  3. DB overlap: ≥80% shared (sample_id, lead_value) rows in leads.db

Outputs:
  - output/duplicates_report.csv — all detected duplicate groups
  - exclude.txt — exact duplicates auto-excluded (keep shortest path)
"""

from __future__ import annotations

import csv
import hashlib
import logging
from pathlib import Path

import fitz  # PyMuPDF
from rapidfuzz import fuzz

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
        doc = fitz.open(str(path))
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def find_duplicates(input_dir: Path, output_dir: Path) -> dict:
    """
    Scan input_dir for duplicate PDFs. Writes report and exclude list.

    Returns a summary dict with counts.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_files = sorted(input_dir.rglob("*.pdf")) + sorted(input_dir.rglob("*.PDF"))

    # Deduplicate path list
    seen_paths: set[Path] = set()
    unique_pdfs: list[Path] = []
    for p in pdf_files:
        resolved = p.resolve()
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            unique_pdfs.append(p)

    _log.info("Scanning %d PDFs for duplicates...", len(unique_pdfs))

    # --- Method 1: Exact MD5 duplicates ---
    hash_groups: dict[str, list[Path]] = {}
    for p in unique_pdfs:
        h = _md5(p)
        hash_groups.setdefault(h, []).append(p)

    exact_groups = {h: paths for h, paths in hash_groups.items() if len(paths) > 1}
    _log.info("Found %d exact duplicate groups", len(exact_groups))

    # --- Method 2: Text similarity ---
    # Extract text for all files, compare pairs within non-exact groups
    texts: dict[Path, str] = {}
    page_counts: dict[Path, int] = {}
    for p in unique_pdfs:
        texts[p] = _extract_text(p)
        page_counts[p] = _page_count(p)

    # Only compare files NOT already in an exact group
    exact_files: set[Path] = set()
    for paths in exact_groups.values():
        exact_files.update(paths)

    remaining = [p for p in unique_pdfs if p not in exact_files]
    text_similar: list[tuple[Path, Path, float, str]] = []

    # For efficiency, only compare files with similar page counts (±2)
    for i, a in enumerate(remaining):
        for b in remaining[i + 1:]:
            if abs(page_counts[a] - page_counts[b]) > 2:
                continue

            text_a, text_b = texts[a], texts[b]
            if text_a and text_b:
                # Both have text — compare
                score = fuzz.ratio(text_a[:5000], text_b[:5000])
                if score >= 90:
                    text_similar.append((a, b, score, "text_similar"))
            elif not text_a and not text_b and page_counts[a] == page_counts[b]:
                # Both scanned, same page count — flag as possible if similar size
                    size_a, size_b = a.stat().st_size, b.stat().st_size
                    size_ratio = min(size_a, size_b) / max(size_a, size_b) if max(size_a, size_b) > 0 else 0
                    if size_ratio >= 0.90:
                        text_similar.append((a, b, size_ratio * 100, "possible_scan_dup"))

    _log.info("Found %d text-similar pairs", len(text_similar))

    # --- Method 3: DB overlap ---
    db_similar: list[tuple[Path, Path, float]] = []
    db_path = Path.cwd() / "leads.db"
    if db_path.exists():
        try:
            from njlead.db.models import Document, Measurement, Sample
            from njlead.db.session import get_session, init_db
            init_db(db_path)

            with get_session() as session:
                # Build a map of filename -> set of (sample_id, result_ppb)
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

                # Compare pairs
                filenames = list(file_rows.keys())
                for i, a in enumerate(filenames):
                    for b in filenames[i + 1:]:
                        rows_a, rows_b = file_rows[a], file_rows[b]
                        overlap = rows_a & rows_b
                        total = min(len(rows_a), len(rows_b))
                        if total > 0 and len(overlap) / total >= 0.80:
                            score = len(overlap) / total * 100
                            db_similar.append((
                                input_dir / a,
                                input_dir / b,
                                score,
                            ))

            _log.info("Found %d DB-overlap pairs", len(db_similar))
        except Exception as e:
            _log.warning("DB overlap check failed: %s", e)

    # --- Write report ---
    report_path = output_dir / "duplicates_report.csv"
    exclude_paths: list[str] = []
    group_id = 0

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "group_id", "match_type", "score", "file_path", "size_bytes", "page_count",
        ])
        writer.writeheader()

        # Exact groups
        for _h, paths in sorted(exact_groups.items()):
            group_id += 1
            # Keep the shortest path, exclude the rest
            paths_sorted = sorted(paths, key=lambda p: len(str(p)))
            for i, p in enumerate(paths_sorted):
                writer.writerow({
                    "group_id": group_id,
                    "match_type": "exact",
                    "score": 100.0,
                    "file_path": str(p.relative_to(input_dir)) if p.is_relative_to(input_dir) else str(p),
                    "size_bytes": p.stat().st_size,
                    "page_count": page_counts.get(p, 0),
                })
                if i > 0:
                    rel = str(p.relative_to(input_dir)) if p.is_relative_to(input_dir) else p.name
                    exclude_paths.append(rel)

        # Text-similar pairs
        for a, b, score, match_type in text_similar:
            group_id += 1
            for p in (a, b):
                writer.writerow({
                    "group_id": group_id,
                    "match_type": match_type,
                    "score": f"{score:.1f}",
                    "file_path": str(p.relative_to(input_dir)) if p.is_relative_to(input_dir) else str(p),
                    "size_bytes": p.stat().st_size,
                    "page_count": page_counts.get(p, 0),
                })

        # DB-overlap pairs
        for a, b, score in db_similar:
            group_id += 1
            for p in (a, b):
                writer.writerow({
                    "group_id": group_id,
                    "match_type": "db_overlap",
                    "score": f"{score:.1f}",
                    "file_path": str(p.relative_to(input_dir)) if p.is_relative_to(input_dir) else str(p),
                    "size_bytes": p.stat().st_size if p.exists() else 0,
                    "page_count": page_counts.get(p, 0),
                })

    _log.info("Wrote %s", report_path)

    # --- Write exclude.txt (exact duplicates only) ---
    if exclude_paths:
        exclude_file = Path.cwd() / "exclude.txt"
        with open(exclude_file, "w", encoding="utf-8") as f:
            f.write("# Auto-generated by njlead find-duplicates\n")
            f.write("# Exact duplicates — the shorter-path copy is kept\n")
            for p in sorted(exclude_paths):
                f.write(p + "\n")
        _log.info("Wrote %s (%d files to exclude)", exclude_file, len(exclude_paths))

    return {
        "total_files": len(unique_pdfs),
        "exact_groups": len(exact_groups),
        "exact_excluded": len(exclude_paths),
        "text_similar_pairs": len(text_similar),
        "db_overlap_pairs": len(db_similar),
    }
