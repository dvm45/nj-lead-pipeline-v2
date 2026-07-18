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
    engine: str = typer.Option(
        "regex",
        "--engine", "-e",
        help="Extraction engine: 'regex' (default, offline) or 'llm' (Claude API).",
    ),
    limit: int = typer.Option(
        None,
        "--limit", "-n",
        help="Only process the first N files. Useful for a cheap test run with --engine llm.",
    ),
) -> None:
    """
    Walk a folder and ingest all PDF files into leads.db.

    Files already in the database (matched by SHA-256 hash) are skipped.
    Parse failures are logged to the issues table — the run never aborts.

    Two engines are available:
      regex (default) — the multi-strategy regex parser. Offline, free.
      llm             — one Claude API call per report. Requires ANTHROPIC_API_KEY.
                        Handles scanned PDFs and unknown lab formats. Costs
                        pennies per report.

    After ingesting, check results with:
      /db-summary        — row counts per table
      /inspect-issues    — review anything that went wrong
    """
    from njlead.db.session import init_db
    from njlead.ingest.loader import ingest_folder

    engine = engine.lower().strip()
    if engine not in {"regex", "llm"}:
        typer.echo(f"Unknown engine '{engine}'. Choose 'regex' or 'llm'.", err=True)
        raise typer.Exit(code=2)

    # Preflight for the LLM engine — fail fast with a friendly message if the
    # user forgot to add their key, instead of failing on the first file.
    if engine == "llm":
        from njlead import config
        if not config.api_key_is_set():
            typer.echo(
                "No Anthropic API key found.\n"
                "\n"
                "The LLM engine needs a key. To set one up:\n"
                "  1) Copy .env.example to .env\n"
                "  2) Put your key in it:  ANTHROPIC_API_KEY=sk-ant-...\n"
                "  3) Re-run this command.\n"
                "\n"
                "Or export the variable in your shell for this session only.\n"
                "Confirm status any time with:  njlead check-llm",
                err=True,
            )
            raise typer.Exit(code=1)

    # Auto-initialize the database if leads.db doesn't exist yet
    db_path = Path.cwd() / "leads.db"
    if not db_path.exists():
        typer.echo("leads.db not found — initializing database first...")
        init_db(db_path)
        typer.echo(f"Database created: {db_path.resolve()}")
        typer.echo("")

    typer.echo(f"Ingesting PDFs from: {folder}")
    typer.echo(f"  engine: {engine}")
    if limit:
        typer.echo(f"  limit:  {limit} file(s)")
    typer.echo("")

    # LLMConfigError propagates out of ingest_folder if e.g. the SDK isn't
    # installed. Catch it here so the user sees a clean setup hint instead
    # of a traceback.
    try:
        counts = ingest_folder(folder, engine=engine, limit=limit)
    except Exception as e:
        # Only the LLM engine can raise here; the regex engine swallows errors
        # per file. We match by class name so the anthropic SDK doesn't have
        # to be importable when running the regex engine.
        if type(e).__name__ == "LLMConfigError":
            typer.echo("", err=True)
            typer.echo(f"LLM setup problem: {e}", err=True)
            raise typer.Exit(code=1)
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

    Spends nothing — never contacts the API. Just prints whether an
    Anthropic API key was found, which model is configured, and what
    confidence threshold the validation gate is using. Safe to run
    before you have a key set up.
    """
    from njlead import config

    key_set = config.api_key_is_set()
    typer.echo("LLM engine configuration")
    typer.echo("-" * 40)
    typer.echo(f"  API key set:           {'yes' if key_set else 'no'}")
    typer.echo(f"  Model:                 {config.LLM_MODEL}")
    typer.echo(f"  Max tokens:            {config.LLM_MAX_TOKENS}")
    typer.echo(f"  Confidence threshold:  {config.CONFIDENCE_THRESHOLD}")
    typer.echo(f"  Scan render DPI:       {config.SCAN_RENDER_DPI}")
    typer.echo("-" * 40)

    # Show whether the SDK itself is importable — that's the other thing that
    # would stop the LLM engine from running (and doesn't need a key to check).
    try:
        import anthropic  # noqa: F401
        sdk_ok = True
    except ImportError:
        sdk_ok = False
    typer.echo(f"  anthropic SDK installed: {'yes' if sdk_ok else 'no'}")

    if not key_set:
        typer.echo("")
        typer.echo("To set the API key:")
        typer.echo("  1) cp .env.example .env")
        typer.echo("  2) Put your key in .env:  ANTHROPIC_API_KEY=sk-ant-...")
        typer.echo("  3) Re-run:  njlead check-llm")
    if not sdk_ok:
        typer.echo("")
        typer.echo("To install the SDK:  pip install -r requirements.txt")
    if key_set and sdk_ok:
        typer.echo("")
        typer.echo("Ready. Try a small test run:")
        typer.echo("  njlead ingest data/ --engine llm --limit 3")


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
        raise typer.Exit(code=1)

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
        output_path = export_to_csv(output_dir=output_dir)
        typer.echo(f"Exported: {output_path.resolve()}")
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
        raise typer.Exit(code=1)
