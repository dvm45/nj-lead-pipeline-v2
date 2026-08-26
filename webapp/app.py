"""
Flask web UI for the NJ lead pipeline.

Provides a browser-based interface so non-technical users can trigger the
pipeline and view status without touching a terminal.

Run locally for development:
    flask --app webapp.app run --debug

On EC2 (production):
    gunicorn -w 1 -b 0.0.0.0:80 webapp.app:app
"""

import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, send_from_directory, url_for

load_dotenv()

app = Flask(__name__)

# --- Paths ----------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_DIR = BASE_DIR / "input_pdfs"
OUTPUT_DIR = BASE_DIR / "output_csvs"
INPUT_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# Google Drive folder IDs (from .env)
GDRIVE_INPUT_FOLDER_ID = os.environ.get("GDRIVE_INPUT_FOLDER_ID", "")
GDRIVE_OUTPUT_FOLDER_ID = os.environ.get("GDRIVE_OUTPUT_FOLDER_ID", "")

# --- Run state (shared with the background thread) -------------------------

run_state = {
    "running": False,
    "step": "idle",
    "detail": "",
    "last_result": None,
    "last_run_time": None,
    "error": None,
}
run_lock = threading.Lock()


# --- Routes ----------------------------------------------------------------

@app.route("/")
def index():
    from njlead.db.models import Document
    from njlead.db.session import get_session

    docs = []
    try:
        with get_session() as session:
            docs = (
                session.query(Document)
                .order_by(Document.ingested_at.desc())
                .limit(50)
                .all()
            )
            # Detach from session so template can read them
            session.expunge_all()
    except Exception:
        pass

    csv_files = sorted(OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)

    return render_template(
        "index.html",
        run_state=run_state,
        documents=docs,
        csv_files=csv_files,
        drive_configured=bool(GDRIVE_INPUT_FOLDER_ID and GDRIVE_OUTPUT_FOLDER_ID),
    )


@app.route("/run", methods=["POST"])
def run_pipeline():
    with run_lock:
        if run_state["running"]:
            return redirect(url_for("index"))
        run_state["running"] = True
        run_state["error"] = None
        run_state["step"] = "starting"
        run_state["detail"] = ""

    thread = threading.Thread(target=_do_pipeline_run, daemon=True)
    thread.start()
    return redirect(url_for("index"))


@app.route("/status")
def status():
    return jsonify(run_state)


@app.route("/downloads/<filename>")
def download_file(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


# --- Background pipeline execution -----------------------------------------

def _do_pipeline_run():
    try:
        # Step 1: Download from Google Drive (if configured)
        if GDRIVE_INPUT_FOLDER_ID:
            run_state["step"] = "downloading"
            run_state["detail"] = "Checking Google Drive for new PDFs..."
            from webapp.drive import download_new_pdfs

            new_files = download_new_pdfs(GDRIVE_INPUT_FOLDER_ID, INPUT_DIR)
            run_state["detail"] = f"Downloaded {len(new_files)} new file(s) from Drive"
        else:
            run_state["detail"] = "No Google Drive configured, using local input_pdfs/ folder"

        # Step 2: Ingest
        run_state["step"] = "ingesting"
        run_state["detail"] = "Processing PDFs..."

        os.environ.setdefault("NJLEAD_DB", str(BASE_DIR / "leads.db"))
        from njlead.db.session import init_db
        from njlead.ingest.loader import ingest_folder

        init_db()
        counts = ingest_folder(INPUT_DIR)
        run_state["detail"] = (
            f"Done: {counts['ingested']} ingested, "
            f"{counts['skipped']} skipped, "
            f"{counts['failed']} failed"
        )

        # Step 3: Export
        run_state["step"] = "exporting"
        run_state["detail"] = "Generating CSV exports..."

        from njlead.export.writer import export_to_csv

        # Per-run timestamped CSV
        run_csv = export_to_csv(output_dir=OUTPUT_DIR)

        # Master CSV (copy the latest export to a fixed name)
        master_path = OUTPUT_DIR / "master.csv"
        shutil.copy2(run_csv, master_path)

        # Done
        run_state["step"] = "done"
        run_state["detail"] = (
            f"Complete! {counts['ingested']} new file(s) processed. "
            f"CSV: {run_csv.name}"
        )
        run_state["last_result"] = counts
        run_state["last_run_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    except Exception as e:
        run_state["step"] = "error"
        run_state["error"] = str(e)
        run_state["detail"] = f"Error: {e}"
    finally:
        run_state["running"] = False
