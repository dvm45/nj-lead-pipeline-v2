"""
Run orchestrator — the engine behind `njlead run`.

Orchestrates the full pipeline in one step:
  1. Download PDFs from Google Drive (or use a local folder)
  2. Ingest into a fresh leads.db
  3. Export to CSV
  4. Merge into master.csv and issues.csv on Drive
  5. Move processed PDFs to the Processed subfolder
  6. Write a last_run.txt summary to Drive

The Drive Processed folder is the dedup mechanism: PDFs already there
are skipped unless --full-refresh is set. A fresh local DB is created
for each run, so reprocessed PDFs always get a clean extraction.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)


def _merge_csv(
    existing_path: Path | None,
    new_path: Path,
    output_path: Path,
    key_column: str,
    source_files_this_run: set[str],
    full_refresh: bool,
) -> int:
    """
    Merge new CSV rows into an existing CSV.

    For incremental runs: remove rows from existing whose key_column value
    matches any file processed this run, then append all new rows. This
    replaces old rows for reprocessed files without duplicating.

    For --full-refresh: ignore existing, just use the new rows.

    Returns the total row count in the merged output.
    """
    if full_refresh or existing_path is None or not existing_path.exists():
        # Full refresh or no prior CSV — just use the new file as-is
        shutil.copy2(new_path, output_path)
        with open(output_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f) - 1  # subtract header

    # Read the new CSV header to ensure column alignment
    with open(new_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        new_fieldnames = reader.fieldnames or []
        new_rows = list(reader)

    # Read existing CSV, dropping rows whose source_file was reprocessed
    kept_rows: list[dict] = []
    with open(existing_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing_fieldnames = reader.fieldnames or []
        for row in reader:
            if row.get(key_column, "") not in source_files_this_run:
                kept_rows.append(row)

    # Use new fieldnames (they may have added columns since last run)
    fieldnames = new_fieldnames

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in kept_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        for row in new_rows:
            writer.writerow(row)

    return len(kept_rows) + len(new_rows)


def run_pipeline(
    *,
    full_refresh: bool = False,
    limit: int | None = None,
    local_folder: Path | None = None,
) -> dict:
    """
    Run the full pipeline end-to-end.

    Returns a summary dict with counts and stats.
    """
    from njlead import config
    from njlead.db.session import init_db
    from njlead.export.writer import export_to_csv
    from njlead.ingest.loader import ingest_folder

    use_drive = local_folder is None
    input_folder_id = os.environ.get("GDRIVE_INPUT_FOLDER_ID", "")
    output_folder_id = os.environ.get("GDRIVE_OUTPUT_FOLDER_ID", "")

    if use_drive and not input_folder_id:
        raise RuntimeError(
            "GDRIVE_INPUT_FOLDER_ID is not set in .env.\n"
            "Either set it to use Google Drive, or use --local <folder> "
            "to process a local folder."
        )
    if use_drive and not output_folder_id:
        raise RuntimeError(
            "GDRIVE_OUTPUT_FOLDER_ID is not set in .env.\n"
            "Set it to the Drive folder where master.csv should go."
        )

    # Work in a temp directory for this run's local files
    run_dir = Path(tempfile.mkdtemp(prefix="njlead_run_"))
    input_dir = run_dir / "input_pdfs"
    output_dir = run_dir / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    summary: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "full_refresh" if full_refresh else "incremental",
        "use_drive": use_drive,
    }
    downloaded_files: list[dict] = []
    processed_file_names: set[str] = set()

    try:
        # --- Step 1: Get PDFs ---
        if use_drive:
            from njlead.drive import download_new_pdfs
            _log.info("Downloading PDFs from Google Drive...")
            print("Downloading PDFs from Google Drive...")
            downloaded_files = download_new_pdfs(
                input_folder_id, input_dir, include_processed=full_refresh,
            )
            pdf_count = len(downloaded_files)
            print(f"  {pdf_count} PDF(s) to process")
            summary["downloaded"] = pdf_count

            if pdf_count == 0:
                print("No new PDFs found. Nothing to do.")
                summary["ingested"] = 0
                summary["skipped"] = 0
                summary["failed"] = 0
                return summary
        else:
            input_dir = local_folder
            summary["local_folder"] = str(local_folder)

        # Apply --limit after download
        if limit is not None and use_drive and len(downloaded_files) > limit:
            downloaded_files = downloaded_files[:limit]
            print(f"  limited to {limit} file(s)")

        # --- Step 2: Ingest into a fresh local DB ---
        db_path = run_dir / "leads.db"
        os.environ["NJLEAD_DB"] = str(db_path)
        init_db(db_path)
        print(f"\nIngesting into fresh database: {db_path.name}")

        counts = ingest_folder(input_dir, limit=limit)
        summary.update(counts)

        # Track which files were processed (for CSV merge dedup)
        for pdf in input_dir.rglob("*.pdf"):
            processed_file_names.add(pdf.name)
        for pdf in input_dir.rglob("*.PDF"):
            processed_file_names.add(pdf.name)

        # --- Step 3: Export to local CSV ---
        if counts.get("ingested", 0) > 0:
            print("\nExporting to CSV...")
            try:
                local_csv = export_to_csv(output_dir=output_dir)
                summary["export_path"] = str(local_csv)
                print(f"  Exported: {local_csv.name}")

                # Count rows
                with open(local_csv, "r", encoding="utf-8") as f:
                    row_count = sum(1 for _ in f) - 1
                summary["rows_exported"] = row_count
            except RuntimeError as e:
                _log.warning("Export failed: %s", e)
                summary["export_error"] = str(e)
        else:
            print("\nNo files ingested — skipping export.")

        # --- Step 3b: Export issues to CSV ---
        issues_csv_path = _export_issues(db_path, output_dir)
        if issues_csv_path:
            with open(issues_csv_path, "r", encoding="utf-8") as f:
                issue_count = sum(1 for _ in f) - 1
            summary["issues_exported"] = issue_count
        else:
            summary["issues_exported"] = 0

        # --- Step 4: Merge into master.csv on Drive ---
        if use_drive and summary.get("export_path"):
            print("\nMerging into master.csv on Drive...")
            local_csv = Path(summary["export_path"])

            # Download existing master.csv (if any)
            from njlead.drive import download_file_by_name, upload_file
            existing_master = output_dir / "existing_master.csv"
            has_existing = False
            if not full_refresh:
                has_existing = download_file_by_name(
                    output_folder_id, "master.csv", existing_master,
                )

            merged_master = output_dir / "master.csv"
            total_rows = _merge_csv(
                existing_master if has_existing else None,
                local_csv,
                merged_master,
                key_column="source_file",
                source_files_this_run=processed_file_names,
                full_refresh=full_refresh,
            )
            summary["master_rows"] = total_rows

            upload_file(merged_master, output_folder_id, overwrite_name="master.csv")
            print(f"  master.csv uploaded ({total_rows} rows)")

            # Merge issues.csv
            if issues_csv_path:
                existing_issues = output_dir / "existing_issues.csv"
                has_existing_issues = False
                if not full_refresh:
                    has_existing_issues = download_file_by_name(
                        output_folder_id, "issues.csv", existing_issues,
                    )
                merged_issues = output_dir / "issues.csv"
                issue_total = _merge_csv(
                    existing_issues if has_existing_issues else None,
                    issues_csv_path,
                    merged_issues,
                    key_column="source_file",
                    source_files_this_run=processed_file_names,
                    full_refresh=full_refresh,
                )
                upload_file(merged_issues, output_folder_id, overwrite_name="issues.csv")
                summary["issues_rows"] = issue_total
                print(f"  issues.csv uploaded ({issue_total} rows)")

        elif local_folder and summary.get("export_path"):
            # Local mode: just copy to working dir
            local_csv = Path(summary["export_path"])
            dest = Path.cwd() / "master.csv"
            shutil.copy2(local_csv, dest)
            print(f"\n  master.csv written to {dest}")

        # --- Step 5: Move processed PDFs to Drive Processed folder ---
        if use_drive and downloaded_files:
            from njlead.drive import move_to_processed
            print("\nMoving processed PDFs to Drive Processed folder...")
            moved = 0
            for f in downloaded_files:
                local_path = f.get("local_path")
                if local_path and Path(local_path).name in processed_file_names:
                    try:
                        move_to_processed(input_folder_id, f)
                        moved += 1
                    except Exception as e:
                        _log.warning("Failed to move %s: %s", f["name"], e)
            summary["moved_to_processed"] = moved
            print(f"  {moved} file(s) moved")

        # --- Step 6: Write last_run.txt ---
        if use_drive:
            from njlead.drive import upload_text
            report_text = _format_run_report(summary)
            upload_text(report_text, output_folder_id, "last_run.txt")
            print("\n  last_run.txt uploaded to Drive output folder")

        print("\nDone!")
        return summary

    finally:
        # Clean up temp DB env var (restore if it was set before)
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
        f"NJ Lead Pipeline — Run Report",
        f"{'=' * 40}",
        f"Timestamp:  {summary.get('timestamp', 'unknown')}",
        f"Mode:       {summary.get('mode', 'unknown')}",
        "",
        "Files",
        f"  Downloaded:  {summary.get('downloaded', 'n/a')}",
        f"  Ingested:    {summary.get('ingested', 0)}",
        f"  Skipped:     {summary.get('skipped', 0)}",
        f"  Failed:      {summary.get('failed', 0)}",
        "",
        "Output",
        f"  Rows in master.csv:  {summary.get('master_rows', summary.get('rows_exported', 0))}",
        f"  Rows in issues.csv:  {summary.get('issues_rows', summary.get('issues_exported', 0))}",
        f"  Moved to Processed:  {summary.get('moved_to_processed', 0)}",
    ]
    return "\n".join(lines) + "\n"
