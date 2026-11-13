"""
Run orchestrator — the engine behind `njlead run`.

Orchestrates the full pipeline in one step:
  1. Download PDFs from Google Drive (or use a local folder)
  2. Ingest into leads.db (persistent, SHA-256 dedup skips already-seen files)
  3. Export to CSV in output/ folder
  4. Write last_run.txt summary

Drive is input-only — all outputs stay local. The DB is persistent across
runs so only new/changed PDFs are processed.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# Token costs per million tokens (as of Nov 2026)
_COST_PER_M_INPUT = {
    "haiku": 0.80,
    "opus": 15.00,
    "sonnet": 3.00,
}
_COST_PER_M_OUTPUT = {
    "haiku": 4.00,
    "opus": 75.00,
    "sonnet": 15.00,
}


def _estimate_cost(token_usage: dict) -> float:
    """Compute estimated dollar cost from per-model token counts."""
    total = 0.0
    for model, counts in token_usage.items():
        inp = counts.get("input", 0)
        out = counts.get("output", 0)
        total += inp / 1_000_000 * _COST_PER_M_INPUT.get(model, 10.0)
        total += out / 1_000_000 * _COST_PER_M_OUTPUT.get(model, 50.0)
    return total


def run_pipeline(
    *,
    full_refresh: bool = False,
    limit: int | None = None,
    random_sample: bool = False,
    local_folder: Path | None = None,
) -> dict:
    """
    Run the full pipeline end-to-end.

    Returns a summary dict with counts and stats.
    """
    from njlead.db.session import init_db
    from njlead.export.writer import export_to_csv
    from njlead.ingest.loader import ingest_folder
    from njlead.token_tracker import tracker

    tracker.reset()
    project_root = Path.cwd()
    use_drive = local_folder is None
    input_folder_id = os.environ.get("GDRIVE_INPUT_FOLDER_ID", "")

    if use_drive and not input_folder_id:
        raise RuntimeError(
            "GDRIVE_INPUT_FOLDER_ID is not set in .env.\n"
            "Either set it to use Google Drive, or use --local <folder> "
            "to process a local folder."
        )

    # Persistent local folder for downloaded PDFs (gitignored)
    input_dir = project_root / "input_pdfs"
    input_dir.mkdir(exist_ok=True)

    # Output folder for CSVs and run logs
    output_dir = project_root / "output"
    output_dir.mkdir(exist_ok=True)

    summary: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": "full_refresh" if full_refresh else "incremental",
        "use_drive": use_drive,
    }

    try:
        # --- Step 1: Get PDFs ---
        if use_drive:
            from njlead.drive import download_new_pdfs
            _log.info("Syncing PDFs from Google Drive...")
            all_drive_files = download_new_pdfs(input_folder_id, input_dir)
            total_on_drive = len(all_drive_files)
            downloaded = sum(1 for f in all_drive_files if not f.get("skipped"))
            summary["drive_total"] = total_on_drive
            summary["downloaded"] = downloaded

            if total_on_drive == 0:
                _log.info("No PDFs found on Drive. Nothing to do.")
                summary["ingested"] = 0
                summary["skipped"] = 0
                summary["failed"] = 0
                return summary
        else:
            input_dir = local_folder
            summary["local_folder"] = str(local_folder)

        # --- Step 2: Handle --full-refresh (back up existing DB) ---
        db_path = project_root / "leads.db"
        if full_refresh and db_path.exists():
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            backup_path = project_root / f"leads_backup_{ts}.db"
            shutil.copy2(db_path, backup_path)
            _log.info("Backed up existing database to %s", backup_path.name)
            db_path.unlink()

        # --- Step 3: Ingest into persistent leads.db ---
        os.environ["NJLEAD_DB"] = str(db_path)
        init_db(db_path)
        _log.info("Ingesting into database: %s", db_path.name)

        counts = ingest_folder(input_dir, limit=limit, random_sample=random_sample)
        summary.update(counts)

        # --- Step 4: Export to output/ ---
        _log.info("Exporting to CSV...")
        try:
            local_csv, duplicates_removed = export_to_csv(output_dir=output_dir)
            summary["export_path"] = str(local_csv)
            summary["duplicates_removed"] = duplicates_removed
            _log.info("  Exported: %s", local_csv.name)

            # Count rows
            with open(local_csv, encoding="utf-8") as f:
                row_count = sum(1 for _ in f) - 1
            summary["rows_exported"] = row_count
        except RuntimeError as e:
            _log.warning("Export failed: %s", e)
            summary["export_error"] = str(e)

        # Copy as master.csv (always a full rebuild from the DB)
        if summary.get("export_path"):
            master_path = output_dir / "master.csv"
            shutil.copy2(Path(summary["export_path"]), master_path)
            _log.info("  master.csv written (%d rows)", summary.get("rows_exported", 0))

        # Export issues
        issues_csv_path = _export_issues(db_path, output_dir)
        if issues_csv_path:
            with open(issues_csv_path, encoding="utf-8") as f:
                issue_count = sum(1 for _ in f) - 1
            summary["issues_exported"] = issue_count
        else:
            summary["issues_exported"] = 0

        # --- Step 5: Record token usage and write last_run.txt ---
        summary["token_usage"] = tracker.summary()
        report_text = _format_run_report(summary)
        last_run_path = output_dir / "last_run.txt"
        last_run_path.write_text(report_text, encoding="utf-8")
        _log.info("  last_run.txt written to output/")

        _log.info("Done!")
        return summary

    except Exception:
        # Write partial last_run.txt even on failure
        try:
            output_dir.mkdir(exist_ok=True)
            summary["error"] = "Run failed — see log for details"
            report_text = _format_run_report(summary)
            (output_dir / "last_run.txt").write_text(report_text, encoding="utf-8")
        except Exception:
            pass
        raise

    finally:
        # Clean up temp DB env var
        if "NJLEAD_DB" in os.environ:
            del os.environ["NJLEAD_DB"]


def _export_issues(db_path: Path, output_dir: Path) -> Path | None:
    """Export all issues from the DB to a CSV file."""
    from njlead.db.models import Document, Issue
    from njlead.db.session import get_session

    with get_session() as session:
        issues = (
            session.query(Issue)
            .join(Document, Issue.document_id == Document.id)
            .all()
        )
        if not issues:
            return None

        issues_path = output_dir / "issues.csv"
        fieldnames = [
            "source_file", "page_num", "issue_type", "detail", "created_at",
        ]
        with open(issues_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for issue in issues:
                doc = session.get(Document, issue.document_id)
                writer.writerow({
                    "source_file": doc.file_name if doc else "",
                    "page_num": issue.page_num if issue.page_num is not None else "",
                    "issue_type": issue.issue_type,
                    "detail": issue.detail or "",
                    "created_at": issue.created_at.isoformat() if issue.created_at else "",
                })

    return issues_path


def _format_run_report(summary: dict) -> str:
    """Format the run summary as human-readable text for last_run.txt."""
    lines = [
        "NJ Lead Pipeline — Run Report",
        f"{'=' * 40}",
        f"Timestamp:  {summary.get('timestamp', 'unknown')}",
        f"Mode:       {summary.get('mode', 'unknown')}",
        "",
        "Files",
    ]

    if summary.get("use_drive"):
        lines.append(f"  On Drive:     {summary.get('drive_total', 'n/a')}")
        lines.append(f"  Downloaded:   {summary.get('downloaded', 'n/a')}")
    elif summary.get("local_folder"):
        lines.append(f"  Local folder: {summary.get('local_folder')}")

    lines.extend([
        f"  Found:       {summary.get('total', 'n/a')}",
        f"  Ingested:    {summary.get('ingested', 0)}",
        f"  Skipped:     {summary.get('skipped', 0)}  (already in database)",
        f"  Failed:      {summary.get('failed', 0)}",
        "",
        "Output",
        f"  Rows in master.csv:  {summary.get('rows_exported', 0)}",
        f"  Duplicates removed:  {summary.get('duplicates_removed', 0)}",
        f"  Rows in issues.csv:  {summary.get('issues_exported', 0)}",
    ])

    # Token usage and cost
    token_usage = summary.get("token_usage", {})
    if token_usage:
        lines.extend(["", "Token usage"])
        for model, counts in sorted(token_usage.items()):
            inp = counts.get("input", 0)
            out = counts.get("output", 0)
            calls = counts.get("calls", 0)
            empty = counts.get("empty_calls", 0)
            line = f"  {model:>8s}:  {inp:>8,} in / {out:>7,} out  ({calls} calls"
            if empty:
                line += f", {empty} empty"
            line += ")"
            lines.append(line)
        cost = _estimate_cost(token_usage)
        lines.append(f"  Estimated cost: ${cost:.2f}")

    if summary.get("error"):
        lines.extend(["", f"ERROR: {summary['error']}"])

    return "\n".join(lines) + "\n"
