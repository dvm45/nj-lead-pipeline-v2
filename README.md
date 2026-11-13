# NJ Lead Pipeline V2

Extracts structured data from NJ school drinking water lead testing PDFs and stores it in a local SQLite database. Each PDF is processed by a multi-agent LLM pipeline that reads reports the way a human analyst would — handling typed text, scanned images, and unfamiliar lab formats alike.

---

## For lab members (quick start)

You need: Windows 10/11, Python 3.11+, and the two credential files Dan sends you (`.env` and `service-account.json`).

### 1. Install Python

Download from [python.org](https://www.python.org/downloads/). **Check "Add Python to PATH"** during install.

### 2. Download and set up

1. Download the repo ZIP and extract it (or `git clone`)
2. Place `.env` in the project folder (right next to `setup.bat`)
3. Create a `credentials/` folder and place `service-account.json` inside it
4. Double-click **`setup.bat`** — it installs everything and downloads reference data

### 3. Run the pipeline

Double-click **`run.bat`**. It downloads new PDFs from Drive, processes them, and writes results to the `output/` folder. A summary prints at the end — keep the window open to check for errors.

### 4. Find your results

All output files are in the `output/` folder inside the project:

- **`output/master.csv`** — all lead results, rebuilt from the database each run
- **`output/export_<timestamp>.csv`** — timestamped snapshot of the same data
- **`output/issues.csv`** — rows that failed validation or were flagged for review
- **`output/last_run.txt`** — run summary (files processed, rows written, cost estimate)

---

## What it does

1. **`njlead run`** — the main command. Downloads PDFs from Google Drive, ingests them into the database, exports to CSV in `output/`. Supports `--full-refresh`, `--limit N`, and `--local <folder>` flags.
2. **`njlead init`** — creates the database (run once before anything else).
3. **`njlead ingest <folder>`** — finds every PDF in a folder and runs each through the 3-agent pipeline.
4. **`njlead refresh-reference`** — downloads NCES school metadata (county, address, coordinates) for every NJ public school.
5. **`njlead export`** — joins the ingested data with NCES metadata and writes a CSV file.
6. **`njlead check-llm`** — reports LLM configuration status (spends nothing).
7. **`njlead find-duplicates`** — detects duplicate PDFs by MD5, text similarity, and DB overlap. Writes `output/duplicates_report.csv` and `exclude.txt`. No Bedrock calls.

### Flags for `njlead run`

| Flag | Purpose |
|---|---|
| `--full-refresh` | Back up the DB, reprocess ALL PDFs, and rebuild master.csv from scratch |
| `--limit N` | Only process the first N files (cheap test run) |
| `--local <folder>` | Skip Drive, process a local folder instead (debugging) |

---

## How the pipeline works

Each PDF goes through three AI agents in sequence:

### Agent 1: Page Classifier (Haiku 4.5)
Filters out noise pages before extraction — chain-of-custody forms, cover letters, lab boilerplate, appendix dividers. Text pages are classified by keyword heuristics (free, instant). Scanned pages are sent as images to Haiku (~$0.001/page). Data pages pass through; noise pages are dropped.

### Agent 2: Extractor (Opus 4.6)
Reads the filtered pages and extracts structured data via a tool-use schema. Handles:
- **Multi-school reports**: district-wide PDFs covering many schools in one file. Per-row school attribution ensures each measurement is assigned to the correct building.
- **First-draw vs flush**: extracts both draw types as separate measurements when the report distinguishes them.
- **Scanned pages**: renders pages as images and reads them with vision.
- **Large reports**: automatically batches reports over 8 text pages or 2 image pages, with adaptive splitting when output nears the token cap.

### Agent 3: Verifier (Sonnet 4.6)
Cross-checks the extraction against a text digest of the source pages. Focuses on:
- Multi-school misattribution (the most common error)
- Value accuracy
- Completeness (missed measurements)
- Quality flags (zip codes parsed as ppb, duplicates)

**Cost-optimized**: verification is skipped for high-confidence, single-school, short reports. When it does run, it logs issues to the issues table but never drops or downgrades rows — all extracted data is kept in the master CSV.

### Validation Gate
Every extracted measurement must pass 5 rule-based checks before being written to the database:
1. **Structure** — required fields present
2. **Range** — physically plausible value (0–50,000 ppb)
3. **Unit** — recognized unit; independently re-derives ppb from the printed value
4. **Source-span** — the printed value must actually appear in the page text (anti-hallucination)
5. **Confidence** — per-row model confidence >= 0.70

Rows that fail any check are logged to the `issues` table for human review — nothing is silently dropped.

---

## Developer setup

### Prerequisites

- Python 3.11+
- AWS Bedrock credentials (or Anthropic API key)
- Google service account JSON with Drive read access

### Install

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"       # includes pytest and ruff
```

### Configure

Copy `.env.example` to `.env` and fill in your credentials. At minimum:
- `AWS_BEARER_TOKEN_BEDROCK` — your Bedrock long-term API key
- `GDRIVE_INPUT_FOLDER_ID` — from the Drive input folder URL
- Place `service-account.json` at `credentials/service-account.json`

### Verify

```
njlead check-llm              # confirms credentials (no API calls)
njlead refresh-reference      # downloads NCES school data (once)
njlead run --local data/ --limit 3   # test run on local PDFs
```

### Tests and linting

```
pytest -v                     # 53 tests, all pass
ruff check njlead/ tests/     # lint
```

---

## Output columns

| Column | Source |
|---|---|
| `county` | NCES EDGE metadata (falls back to folder path) |
| `district` | NCES canonical name (falls back to PDF) |
| `school_name` | NCES canonical name (falls back to PDF) |
| `address` | NCES metadata (falls back to PDF-extracted address) |
| `coordinates` | NCES lat/long |
| `sample_id` | Extracted from PDF |
| `sample_location` | Extracted from PDF |
| `fixture_type` | Extracted from PDF (e.g. "Sink", "Water Cooler") |
| `draw_type` | `first_draw`, `flush`, or blank |
| `sample_date` | Extracted from PDF, ISO format |
| `year` | Year from sample date or test_year (falls back to report_year, filename, folder) |
| `report_year` | The year the report was issued (from cover/header) |
| `year_source` | Where the year came from: `sample_date`, `test_year`, `report_year`, `filename`, or `folder` |
| `lead_concentration_ppb` | Lead result in ppb |
| `confidence` | Model's per-row confidence (0–1) |
| `source_page` | Page number in the source PDF |
| `source_file` | PDF filename |

---

## Debugging tools

Claude Code slash commands for inspecting the database:

| Command | What it shows |
|---|---|
| `/db-summary` | Row counts for all tables, issue breakdown |
| `/inspect-issues` | All extraction problems grouped by type |
| `/show-sample report.pdf 0` | Raw extracted text from page 0 of a file |

---

## Understanding extraction issues

The pipeline logs problems to the `issues` table instead of crashing:

| Issue type | What it means |
|---|---|
| `needs_review` | Row failed validation (bad unit, out-of-range, low confidence, or value not found in source text) |
| `verification` | Sonnet verifier flagged something (wrong school attribution, missed measurements, value discrepancy) |
| `no_school_found` | Model didn't return a school name |
| `no_measurements` | Model returned zero measurements (file may be a cover letter) |
| `llm_error` | API call or response parsing failed |
| `extraction_failed` | PDF couldn't be opened |

---

## Cost

The pipeline uses three Claude models at different price points:

| Agent | Model | Typical cost per report |
|---|---|---|
| Classifier | Haiku 4.5 | ~$0.001/scanned page (text pages are free) |
| Extractor | Opus 4.6 | ~$0.05–0.50 depending on page count |
| Verifier | Sonnet 4.6 | ~$0.01–0.05 (skipped for simple reports) |

Prompt caching reduces repeated system prompt costs by ~90% within a batch run. Use `--limit N` for a trial run before committing to a large folder. Token usage and cost estimate are written to `output/last_run.txt` after each run.

---

## Tuning

Settings in `.env` (all optional — safe defaults are used):

| Setting | Default | Purpose |
|---|---|---|
| `NJLEAD_LLM_PROVIDER` | `bedrock` | `bedrock` or `anthropic` |
| `NJLEAD_BEDROCK_MODEL_ID` | `us.anthropic.claude-opus-4-6-v1` | Bedrock model for extraction (switch to Sonnet to cut cost) |
| `NJLEAD_LLM_MAX_TOKENS` | `16384` | Max response size (tokens) |
| `NJLEAD_CONFIDENCE_THRESHOLD` | `0.70` | Rows below this go to `needs_review` |
| `NJLEAD_SCAN_DPI` | `150` | Resolution for rendering scanned pages |

---

## Project structure

```
nj_lead_pipeline_v2/
├── requirements.txt            <- pinned lock file from working install
├── pyproject.toml              <- makes `njlead` a CLI command
├── .env.example                <- credential template (copy to .env)
├── setup.bat                   <- one-click setup for lab members
├── run.bat                     <- one-click run for lab members
├── CLAUDE.md                   <- developer context for Claude Code
├── CHANGELOG.md
├── njlead/
│   ├── cli.py                  <- Typer CLI commands
│   ├── config.py               <- env/.env settings
│   ├── runner.py               <- `njlead run` orchestrator
│   ├── drive.py                <- Google Drive integration (read-only)
│   ├── logging_setup.py        <- logging config
│   ├── token_tracker.py        <- per-model token usage tracking
│   ├── duplicates.py           <- duplicate file detection
│   ├── db/
│   │   ├── models.py           <- SQLAlchemy ORM (6 tables)
│   │   └── session.py          <- engine + Session factory
│   ├── ingest/
│   │   ├── loader.py           <- folder walk + per-file orchestration
│   │   ├── extractor.py        <- PyMuPDF text extraction
│   │   ├── page_classifier.py  <- Agent 1: Haiku page classifier
│   │   ├── schema.py           <- Pydantic extraction schema
│   │   ├── llm_extractor.py    <- Agent 2: Opus extraction client
│   │   ├── verifier.py         <- Agent 3: Sonnet verification
│   │   ├── validation.py       <- Rule-based QA/QC gate (5 checks)
│   │   ├── llm_pipeline.py     <- orchestrates the full pipeline
│   │   └── year_parser.py      <- year extraction from filenames/folders
│   ├── reference/
│   │   ├── downloader.py       <- fetches NCES CCD + EDGE files
│   │   └── matcher.py          <- fuzzy school-name matcher
│   └── export/
│       └── writer.py           <- CSV export with NCES join + dedup
├── output/                     <- all outputs (gitignored)
├── tests/                      <- pytest tests (53 tests)
└── data/
    ├── (your PDFs here)
    └── reference/              <- cached NCES files
```
