"""
Command-line interface for the NJ lead pipeline.

This file defines four commands that you run in the terminal:

  njlead init                — create leads.db and all tables
  njlead ingest <folder>     — extract data from all PDFs in a folder
  njlead refresh-reference   — download NCES school metadata (county, address,
                               coordinates) and build the local lookup file
  njlead export              — write all data to a CSV file

Typer handles argument parsing and help text automatically.
Run `njlead --help` or `njlead ingest --help` to see usage.
"""

from pathlib import Path

import typer

# The Typer app object — all commands are registered on this
app = typer.Typer(
    name="njlead",
    help="NJ school lead testing PDF extraction pipeline.",
    add_completion=False,  # Disable shell completion (simplifies setup)
)


@app.callback()
def _startup() -> None:
    """Initialize logging before any command runs."""
    from njlead.logging_setup import setup_logging
    setup_logging()


# ---------------------------------------------------------------------------
# njlead run
# ---------------------------------------------------------------------------

@app.command()
def run(
    full_refresh: bool = typer.Option(
        False,
        "--full-refresh",
        help="Reprocess ALL PDFs (including already-processed ones) and rebuild master.csv from scratch.",
    ),
    limit: int = typer.Option(
        None,
        "--limit", "-n",
        help="Only process the first N files. Useful for a cheap test run.",
    ),
    local: Path = typer.Option(
        None,
        "--local",
        help="Skip Google Drive and process a local folder instead (for debugging).",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    download_only: bool = typer.Option(
        False,
        "--download-only",
        help="Download PDFs from Drive but do NOT run extraction. No Bedrock calls.",
    ),
) -> None:
    """
    Run the full pipeline in one step.

    Downloads PDFs from Google Drive (skipping files in exclude.txt and
    files already cached locally), ingests new files into the persistent
    leads.db, exports master.csv and issues.csv to the output/ folder,
    and writes a run summary.

    Use --download-only to sync PDFs from Drive without any Bedrock
    calls (useful before running find-duplicates on local copies).

    For local debugging, use --local <folder> to skip Drive entirely.
    """
    if download_only:
        _run_download_only()
        return

    from njlead import config

    # Preflight: check LLM credentials
    if not config.api_key_is_set():
        typer.echo(
            "LLM credentials not found. Run 'njlead check-llm' for setup help.",
            err=True,
        )
        raise typer.Exit(code=1)

    from njlead.runner import run_pipeline

    try:
        summary = run_pipeline(
            full_refresh=full_refresh,
            limit=limit,
            local_folder=local,
        )
    except FileNotFoundError as e:
        typer.echo(f"\n{e}", err=True)
        raise typer.Exit(code=1) from None
    except RuntimeError as e:
        typer.echo(f"\nError: {e}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as e:
        if type(e).__name__ == "LLMConfigError":
            typer.echo(f"\nLLM setup problem: {e}", err=True)
            raise typer.Exit(code=1) from None
        raise

    # Print final summary
    typer.echo("")
    typer.echo("=" * 40)
    typer.echo(f"  Ingested:    {summary.get('ingested', 0)}")
    typer.echo(f"  Skipped:     {summary.get('skipped', 0)}")
    typer.echo(f"  Failed:      {summary.get('failed', 0)}")
    typer.echo(f"  CSV rows:    {summary.get('rows_exported', 0)}")
    typer.echo(f"  Duplicates:  {summary.get('duplicates_removed', 0)}")
    typer.echo(f"  Issues:      {summary.get('issues_exported', 0)}")
    typer.echo("=" * 40)
    typer.echo("  Output:      output/master.csv")


def _run_download_only() -> None:
    """Download PDFs from Drive without running extraction."""
    import os

    from njlead.drive import download_new_pdfs

    folder_id = os.environ.get("GDRIVE_INPUT_FOLDER_ID", "")
    if not folder_id:
        typer.echo(
            "GDRIVE_INPUT_FOLDER_ID is not set in .env.\n"
            "Set it to the Drive folder URL's folder ID.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    local_dir = Path.cwd() / "input_pdfs"
    local_dir.mkdir(exist_ok=True)

    typer.echo("Downloading PDFs from Drive (no Bedrock calls)...")
    all_files = download_new_pdfs(folder_id, local_dir)
    downloaded = sum(1 for f in all_files if not f.get("skipped"))
    skipped = sum(1 for f in all_files if f.get("skipped"))

    typer.echo("")
    typer.echo("=" * 40)
    typer.echo(f"  Total on Drive:  {len(all_files)}")
    typer.echo(f"  Downloaded:      {downloaded}")
    typer.echo(f"  Skipped:         {skipped}  (cached or excluded)")
    typer.echo("=" * 40)
    typer.echo(f"  Local folder:    {local_dir}")
    typer.echo("")
    typer.echo("Next: njlead find-duplicates   (check for duplicates)")
    typer.echo("Then: njlead run               (run extraction)")


# ---------------------------------------------------------------------------
# njlead init
# ---------------------------------------------------------------------------

@app.command()
def init() -> None:
    """
    Create leads.db and all database tables.

    Run this once before using `ingest` or `export`.
    It's safe to run again — existing data won't be deleted.
    """
    # Import here so startup is fast even if dependencies aren't installed yet
    from njlead.db.session import init_db

    db_path = init_db()
    typer.echo(f"Database initialized: {db_path.resolve()}")
    typer.echo("Tables created: documents, pages, schools, samples, measurements, issues")
    typer.echo("")
    typer.echo("Next step: njlead ingest <folder>")


# ---------------------------------------------------------------------------
# njlead ingest
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    folder: Path = typer.Argument(
        ...,
        help="Path to the folder containing PDF files. Subfolders are searched too.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    limit: int = typer.Option(
        None,
        "--limit", "-n",
        help="Only process the first N files. Useful for a cheap trial run.",
    ),
) -> None:
    """
    Walk a folder and ingest all PDF files into leads.db.

    One Claude API call per report — reads text and scanned pages,
    fills our schema, and only writes rows that clear the validation
    gate. Requires ANTHROPIC_API_KEY (see `njlead check-llm`).

    Files already in the database (matched by SHA-256 hash) are skipped.
    Extraction problems are logged to the issues table — the run never aborts.

    After ingesting, check results with:
      /db-summary        — row counts per table
      /inspect-issues    — review anything that went wrong
    """
    from njlead import config
    from njlead.db.session import init_db
    from njlead.ingest.loader import ingest_folder

    # Preflight — fail fast with a friendly message if the user hasn't set up
    # credentials for whichever provider is selected, instead of failing on
    # the first file.
    if not config.api_key_is_set():
        if config.LLM_PROVIDER == "bedrock":
            typer.echo(
                "No AWS credentials found for Bedrock.\n"
                "\n"
                "The pipeline needs AWS credentials in one of these places:\n"
                "  1) Set AWS_BEARER_TOKEN_BEDROCK in your .env file\n"
                "  2) OR run:  aws configure   (writes ~/.aws/credentials)\n"
                "  3) OR set env vars: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY\n"
                "\n"
                f"Region currently set to: {config.AWS_REGION}\n"
                "Confirm status any time with:  njlead check-llm",
                err=True,
            )
        else:
            typer.echo(
                "No Anthropic API key found.\n"
                "\n"
                "The pipeline needs a key. To set one up:\n"
                "  1) Copy .env.example to .env\n"
                "  2) Put your key in it:  ANTHROPIC_API_KEY=sk-ant-...\n"
                "  3) Re-run this command.\n"
                "\n"
                "Or export the variable in your shell for this session only.\n"
                "Confirm status any time with:  njlead check-llm",
                err=True,
            )
        raise typer.Exit(code=1) from None

    # Auto-initialize the database if leads.db doesn't exist yet
    db_path = Path.cwd() / "leads.db"
    if not db_path.exists():
        typer.echo("leads.db not found — initializing database first...")
        init_db(db_path)
        typer.echo(f"Database created: {db_path.resolve()}")
        typer.echo("")

    typer.echo(f"Ingesting PDFs from: {folder}")
    if limit:
        typer.echo(f"  limit:  {limit} file(s)")
    typer.echo("")

    # LLMConfigError propagates out of ingest_folder if the SDK isn't installed.
    # We match by class name so this except doesn't require the anthropic SDK
    # to be importable at all (the config check above only tests the key).
    try:
        counts = ingest_folder(folder, limit=limit)
    except Exception as e:
        if type(e).__name__ == "LLMConfigError":
            typer.echo("", err=True)
            typer.echo(f"LLM setup problem: {e}", err=True)
            raise typer.Exit(code=1) from None
        raise

    # Print summary
    typer.echo("")
    typer.echo("-" * 40)
    typer.echo(f"  Total found:  {counts['total']}")
    typer.echo(f"  Ingested:     {counts['ingested']}")
    typer.echo(f"  Skipped:      {counts['skipped']}  (already in database)")
    typer.echo(f"  Failed:       {counts['failed']}")
    typer.echo("-" * 40)

    if counts["failed"] > 0:
        typer.echo("")
        typer.echo("Some files had problems. Run /inspect-issues to see details.")

    if counts["ingested"] > 0:
        typer.echo("")
        typer.echo("Next step: njlead export")


# ---------------------------------------------------------------------------
# njlead check-llm
# ---------------------------------------------------------------------------

@app.command(name="check-llm")
def check_llm() -> None:
    """
    Report LLM configuration status.

    Spends nothing — never contacts the API. Just prints which provider
    is selected, whether credentials were found, which model is configured,
    and what confidence threshold the validation gate is using. Safe to run
    before you have anything set up.
    """
    from njlead import config

    creds_ok = config.api_key_is_set()

    typer.echo("LLM engine configuration")
    typer.echo("-" * 40)
    typer.echo(f"  Provider:              {config.LLM_PROVIDER}")

    if config.LLM_PROVIDER == "bedrock":
        typer.echo(f"  AWS region:            {config.AWS_REGION}")
        typer.echo(f"  Bedrock model ID:      {config.BEDROCK_MODEL_ID}")
        typer.echo(f"  AWS credentials found: {'yes' if creds_ok else 'no'}")
    else:
        typer.echo(f"  Anthropic model:       {config.ANTHROPIC_MODEL}")
        typer.echo(f"  API key set:           {'yes' if creds_ok else 'no'}")

    typer.echo(f"  Max tokens:            {config.LLM_MAX_TOKENS}")
    typer.echo(f"  Confidence threshold:  {config.CONFIDENCE_THRESHOLD}")
    typer.echo(f"  Scan render DPI:       {config.SCAN_RENDER_DPI}")
    typer.echo("-" * 40)

    # SDK checks — the anthropic SDK is required for both providers; boto3
    # is only required for Bedrock.
    try:
        import anthropic  # noqa: F401
        sdk_ok = True
    except ImportError:
        sdk_ok = False
    typer.echo(f"  anthropic SDK installed: {'yes' if sdk_ok else 'no'}")

    boto_ok = True  # Only relevant for Bedrock; assume fine otherwise
    if config.LLM_PROVIDER == "bedrock":
        try:
            import boto3  # noqa: F401
            boto_ok = True
        except ImportError:
            boto_ok = False
        typer.echo(f"  boto3 installed:         {'yes' if boto_ok else 'no'}")

    # Setup hints for whatever piece is missing
    if not creds_ok:
        typer.echo("")
        if config.LLM_PROVIDER == "bedrock":
            typer.echo("To provide AWS credentials, pick one:")
            typer.echo("  - Set AWS_BEARER_TOKEN_BEDROCK in your .env file")
            typer.echo("  - aws configure       (writes ~/.aws/credentials)")
            typer.echo("  - set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY env vars")
        else:
            typer.echo("To set the API key:")
            typer.echo("  1) copy .env.example .env")
            typer.echo("  2) Put your key in .env:  ANTHROPIC_API_KEY=sk-ant-...")
            typer.echo("  3) Re-run:  njlead check-llm")

    if not sdk_ok or not boto_ok:
        typer.echo("")
        typer.echo("To install missing packages:  pip install -r requirements.txt")

    if creds_ok and sdk_ok and boto_ok:
        typer.echo("")
        typer.echo("Ready. Try a small test run first:")
        typer.echo("  njlead ingest data/ --limit 3")


# ---------------------------------------------------------------------------
# njlead refresh-reference
# ---------------------------------------------------------------------------

@app.command(name="refresh-reference")
def refresh_reference(
    year: int = typer.Option(
        None,
        "--year", "-y",
        help="School year ending (e.g. 2024 = 2023-24 file). Defaults to the latest released.",
    ),
) -> None:
    """
    Download NCES reference data (school directory + EDGE geocodes) for NJ.

    This pulls two files from nces.ed.gov, filters them to New Jersey,
    and joins them into a single lookup CSV at:

        data/reference/nj_schools_<YEAR>.csv

    Run this once before your first export. Re-run when NCES publishes
    a newer school year (typically each summer). Exports work fully
    offline once this file is cached.
    """
    from njlead.reference import LATEST_REFERENCE_YEAR
    from njlead.reference.downloader import refresh_all

    target_year = year if year is not None else LATEST_REFERENCE_YEAR
    dest_dir = Path.cwd() / "data" / "reference"

    typer.echo(f"Refreshing NCES reference data for school year {target_year - 1}-{target_year}")
    typer.echo(f"  destination: {dest_dir}")
    typer.echo("")

    try:
        lookup = refresh_all(target_year, dest_dir)
    except Exception as e:
        typer.echo("", err=True)
        typer.echo(f"Refresh failed: {e}", err=True)
        typer.echo(
            "If a download URL has 404'd, NCES may have published a newer file.\n"
            "Edit njlead/reference/downloader.py and update the URL constants.",
            err=True,
        )
        raise typer.Exit(code=1) from None

    typer.echo("")
    typer.echo(f"Done. Lookup file: {lookup.resolve()}")
    typer.echo("Next step: njlead export")


# ---------------------------------------------------------------------------
# njlead export
# ---------------------------------------------------------------------------

@app.command()
def export(
    output_dir: Path = typer.Option(
        None,
        "--output-dir", "-o",
        help="Directory to write the CSV file (default: current directory).",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
) -> None:
    """
    Export all measurements to a CSV file.

    Writes export_YYYY-MM-DD_HHMMSS.csv to the current directory
    (or --output-dir if specified).

    Each row = one lead measurement, with school, location, date, and result.
    """
    from njlead.export.writer import export_to_csv

    try:
        output_path, duplicates_removed = export_to_csv(output_dir=output_dir)
        typer.echo(f"Exported: {output_path.resolve()}")
        if duplicates_removed:
            typer.echo(f"  ({duplicates_removed} duplicate rows removed)")
        typer.echo("Open the CSV in Excel or any spreadsheet app to review the data.")

        # Surface the unmatched-schools log if the export wrote one.
        # These are schools the NCES matcher couldn't resolve — coordinates
        # and address columns will be blank for their rows.
        unmatched_path = Path.cwd() / "data" / "reference" / "unmatched_schools.csv"
        if unmatched_path.exists():
            typer.echo("")
            typer.echo(
                f"Note: some schools didn't match the NCES reference.\n"
                f"  See {unmatched_path.resolve()}"
            )
    except RuntimeError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from None


# ---------------------------------------------------------------------------
# njlead find-duplicates
# ---------------------------------------------------------------------------

@app.command(name="find-duplicates")
def find_duplicates(
    input_dir: Path = typer.Option(
        None,
        "--input-dir", "-i",
        help="Folder with local PDFs (default: input_pdfs/). Local checks skipped if folder doesn't exist yet.",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    skip_drive: bool = typer.Option(
        False,
        "--skip-drive",
        help="Skip the Drive check and only scan local files.",
    ),
) -> None:
    """
    Detect duplicate PDFs before running the pipeline. No Bedrock calls.

    First checks the Drive input folder by md5Checksum (no download
    required). Then scans local copies for text similarity and DB overlap.

    Writes output/duplicates_report.csv and auto-generates exclude.txt
    for exact duplicates. Both `njlead run` and `njlead ingest` read
    exclude.txt and skip listed files.

    Recommended workflow:
      1. njlead find-duplicates   (review the report)
      2. njlead run               (skips excluded files)
    """
    import os

    from njlead.duplicates import find_all_duplicates
    from njlead.duplicates import find_duplicates as do_find_local

    if input_dir is None:
        input_dir = Path.cwd() / "input_pdfs"
    output_dir = Path.cwd() / "output"

    # Drive exact check (no download, no cost)
    drive_files: list[dict] = []
    if not skip_drive:
        folder_id = os.environ.get("GDRIVE_INPUT_FOLDER_ID", "")
        if folder_id:
            try:
                from njlead.drive import list_drive_pdfs
                typer.echo("Listing Drive input folder...")
                drive_files = list_drive_pdfs(folder_id)
                typer.echo(f"  Found {len(drive_files)} PDFs on Drive")
            except Exception as e:
                typer.echo(f"  Drive listing failed: {e}", err=True)
                typer.echo("  Falling back to local-only check.")
        else:
            typer.echo("  GDRIVE_INPUT_FOLDER_ID not set — skipping Drive check.")

    # If we have Drive files, use the full workflow
    if drive_files:
        local_dir = input_dir if input_dir.exists() else None
        result = find_all_duplicates(drive_files, local_dir, output_dir)
        typer.echo("")
        typer.echo("=" * 40)
        typer.echo(f"  Drive files:       {result['drive_files']}")
        typer.echo(f"  Drive dup groups:  {result['drive_exact_groups']}")
        typer.echo(f"  Drive excluded:    {result['drive_exact_excluded']}")
        if result["local_files"] > 0:
            typer.echo(f"  Local files:       {result['local_files']}")
            typer.echo(f"  Local dup groups:  {result['exact_groups']}")
            typer.echo(f"  Text-similar:      {result['text_similar_pairs']}  (review in report)")
            typer.echo(f"  DB overlap:        {result['db_overlap_pairs']}  (review in report)")
        else:
            typer.echo("  (no local files — run 'njlead run --download-only' to sync)")
        total_excluded = result["drive_exact_excluded"] + result["exact_excluded"]
    else:
        # Local-only fallback
        if not input_dir.exists():
            typer.echo(f"Input folder not found: {input_dir}", err=True)
            typer.echo("Run 'njlead run --download-only' first to download PDFs.")
            raise typer.Exit(code=1) from None
        result = do_find_local(input_dir, output_dir)
        typer.echo("")
        typer.echo("=" * 40)
        typer.echo(f"  Files scanned:     {result['total_files']}")
        typer.echo(f"  Exact dup groups:  {result['exact_groups']}")
        typer.echo(f"  Auto-excluded:     {result['exact_excluded']}")
        typer.echo(f"  Text-similar:      {result['text_similar_pairs']}  (review in report)")
        typer.echo(f"  DB overlap:        {result['db_overlap_pairs']}  (review in report)")
        total_excluded = result["exact_excluded"]

    typer.echo("=" * 40)
    typer.echo("  Report: output/duplicates_report.csv")
    if total_excluded > 0:
        typer.echo(f"  Exclude: exclude.txt ({total_excluded} files)")
