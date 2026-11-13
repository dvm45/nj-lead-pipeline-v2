# CLAUDE.md — developer context for the NJ Lead Pipeline

## What this project does

Extracts structured lead-in-water testing data from NJ school report PDFs using a 3-agent LLM pipeline (Haiku classifier → Opus extractor → Sonnet verifier), stores results in a persistent SQLite database, and exports to CSV. Designed for non-technical lab members who double-click bat files on Windows.

## Architecture

```
njlead/
├── cli.py              ← Typer CLI: init, ingest, run, export, check-llm, refresh-reference, find-duplicates
├── config.py           ← All settings from env vars / .env (provider, model, thresholds)
├── runner.py           ← `njlead run` orchestrator (Drive download → ingest → export)
├── drive.py            ← Google Drive integration (read-only: service account auth, download)
├── logging_setup.py    ← Console + per-run log file handlers
├── token_tracker.py    ← Per-model token usage and cost tracking
├── duplicates.py       ← Duplicate file detection (MD5, text similarity, DB overlap)
├── db/
│   ├── models.py       ← SQLAlchemy ORM: documents, pages, schools, samples, measurements, issues
│   └── session.py      ← Engine + Session factory, auto-init
├── ingest/
│   ├── loader.py       ← Folder walk, SHA-256 dedup, exclude.txt support, per-file orchestration
│   ├── extractor.py    ← PyMuPDF text extraction (raw text per page)
│   ├── page_classifier.py  ← Agent 1: keyword heuristics (text) + Haiku image calls (scanned)
│   ├── schema.py       ← Pydantic extraction schema (the "blank form" for tool-use)
│   ├── llm_extractor.py    ← Agent 2: Opus extraction client, 8-page batching, adaptive retry
│   ├── verifier.py     ← Agent 3: Sonnet cross-check (log-only, no confidence downgrading)
│   ├── llm_pipeline.py ← Orchestrates classify → extract → verify → validate → write
│   ├── validation.py   ← 5-check rule-based gate (structure, range, unit, source-span, confidence)
│   └── year_parser.py  ← Year extraction from filenames and folder paths (fallback chain)
├── reference/
│   ├── downloader.py   ← NCES CCD + EDGE file download/join (EDGE NMCNTY for county)
│   └── matcher.py      ← Fuzzy school-name matcher (rapidfuzz, block-by-district)
└── export/
    └── writer.py       ← CSV export with NCES join, lead-only filter, dedup, year_source
```

## Outputs

All outputs go to `output/` inside the repo folder (gitignored):

- `output/master.csv` — rebuilt from DB each run, all lead results
- `output/export_<timestamp>.csv` — timestamped snapshot of the same data
- `output/issues.csv` — rows that failed validation or were flagged
- `output/last_run.txt` — run summary with token usage and cost estimate
- `output/duplicates_report.csv` — from `njlead find-duplicates`
- `exclude.txt` — auto-generated list of duplicate files to skip during ingest

## Key design decisions

- **Persistent DB with SHA-256 dedup**: `leads.db` persists across runs. Files already in the DB (by SHA-256) are skipped. `--full-refresh` backs up the DB and starts fresh.
- **Drive is input-only**: Drive scope is read-only. PDFs are downloaded; nothing is uploaded or moved. MD5 checksums skip re-downloading cached files.
- **Bedrock default, Anthropic optional**: set via `NJLEAD_LLM_PROVIDER` in `.env`. Both use the same anthropic SDK. Extractor model is configurable via `NJLEAD_BEDROCK_MODEL_ID`.
- **Errors → issues table, never crash a batch**: individual file failures are caught, logged, and counted. Only `LLMConfigError` (missing credentials) aborts the whole run.
- **Validation gate**: every extracted row passes 5 checks before DB write. Failed rows go to the `issues` table for human review.
- **Conditional verification**: Sonnet verification is skipped for high-confidence, single-school, short reports. When it runs, it logs issues but never downgrades confidence — all rows are kept in the master.
- **Year fallback chain**: LLM report_year → max(sample dates) → filename parse → folder path. `year_source` column tracks which source was used.
- **Export dedup**: rows are deduplicated on (school_name, sample_id, sample_location, draw_type, sample_date, lead_concentration_ppb).

## Models

| Agent | Model | Bedrock ID | Purpose |
|---|---|---|---|
| Classifier | Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Filter noise pages |
| Extractor | Opus 4.6 | `us.anthropic.claude-opus-4-6-v1` | Structured data extraction |
| Verifier | Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | Cross-check extraction |

## Coding conventions

- `pathlib.Path` for every file path (no raw strings)
- Log failures to the `issues` table instead of crashing
- Comments on non-obvious steps
- Short single-purpose functions
- No premature abstraction
- Never commit secrets (`.env`, `credentials/`, `leads.db`, PDFs, exports are gitignored)

## Running tests

```
pip install -e ".[dev]"
pytest -v
ruff check njlead/ tests/
```

## CSV output columns (in order)

county, district, school_name, address, coordinates, sample_id, sample_location, fixture_type, draw_type, sample_date, year, report_year, year_source, lead_concentration_ppb, confidence, source_page, source_file

## Debugging slash commands (Claude Code)

- `/db-summary` — row counts for all tables, issue breakdown
- `/inspect-issues` — all extraction problems grouped by type
- `/show-sample report.pdf 0` — raw extracted text from page 0
