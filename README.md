# NJ Lead Pipeline V2

Extracts structured data from NJ school drinking water lead testing PDFs and stores it in a local SQLite database. Each PDF is processed by a multi-agent LLM pipeline that reads reports the way a human analyst would — handling typed text, scanned images, and unfamiliar lab formats alike.

---

## What it does

1. **`njlead init`** — creates the database (run once before anything else).
2. **`njlead ingest <folder>`** — finds every PDF in a folder, runs each through the 3-agent pipeline, and writes validated results to `leads.db`.
3. **`njlead refresh-reference`** — downloads NCES school metadata (county, address, coordinates) for every NJ public school. Run once before your first export, then yearly.
4. **`njlead export`** — joins the ingested data with NCES metadata and writes a CSV file you can open in Excel.

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
- **Large reports**: automatically batches reports over 12 text pages or 4 image pages, with adaptive splitting when output nears the token cap.

### Agent 3: Verifier (Sonnet 4.6)
Cross-checks the extraction against a text digest of the source pages. Focuses on:
- Multi-school misattribution (the most common error)
- Value accuracy
- Completeness (missed measurements)
- Quality flags (zip codes parsed as ppb, duplicates)

**Cost-optimized**: verification is skipped for high-confidence, single-school, short reports where the extractor is unlikely to have erred. When it does run, it receives a compact text digest — not all source pages as images.

### Validation Gate
Every extracted measurement must pass 5 rule-based checks before being written to the database:
1. **Structure** — required fields present
2. **Range** — physically plausible value (0–50,000 ppb)
3. **Unit** — recognized unit; independently re-derives ppb from the printed value
4. **Source-span** — the printed value must actually appear in the page text (anti-hallucination)
5. **Confidence** — per-row model confidence >= 0.70

Rows that fail any check are logged to the `issues` table for human review — nothing is silently dropped.

---

## Setup (Windows)

### Step 1 — Python 3.11+

```
python --version
```

If not 3.11 or higher, download from [python.org](https://www.python.org/downloads/) and check "Add Python to PATH" during install.

### Step 2 — Open a terminal in the project folder

Right-click the `nj_lead_pipeline_v2` folder → "Open in Terminal"

### Step 3 — Create and activate a virtual environment

```
python -m venv .venv
.venv\Scripts\activate
```

Your prompt should show `(.venv)`. You need to activate this every time you open a new terminal.

### Step 4 — Install dependencies

```
pip install -r requirements.txt
pip install -e .
```

### Step 5 — Configure AWS Bedrock credentials

The pipeline calls Claude via AWS Bedrock. You need AWS credentials with Bedrock model access:

```
aws configure
```

Or set environment variables:
```
set AWS_ACCESS_KEY_ID=your-key
set AWS_SECRET_ACCESS_KEY=your-secret
set AWS_REGION=us-east-2
```

**Alternative: Anthropic direct API.** Set `NJLEAD_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=sk-ant-...` in a `.env` file.

### Step 6 — Verify

```
njlead --help
njlead check-llm
```

`check-llm` confirms credentials and SDK are set up. It makes no API calls and costs nothing.

---

## Usage

### Initialize the database (first time only)

```
njlead init
```

### Ingest PDFs

Start with a small trial run:

```
njlead ingest data/ --limit 3
```

Then run the full folder:

```
njlead ingest data/
```

Output example:

```
Found 229 PDF file(s) in data/
  [1/229] Atlantic County report.pdf ... [llm] pages=3 measurements=34
    [verify] sonnet in=4951 out=347  multi_school=False  confidence=0.97
  ingested
  [2/229] Bridgeton BOE Summary.pdf ... [filter] dropped 2 noise page(s)
    [llm] pages=2 measurements=110  school='Bridgeton Public Schools'
    [verify] sonnet in=5969 out=543  multi_school=True  attribution_ok=True
  ingested
  ...
```

### Export to CSV

```
njlead export
```

Writes `export_<timestamp>.csv` with columns:

| Column | Source |
|---|---|
| `county` | NCES metadata |
| `district` | NCES canonical name (falls back to PDF) |
| `school_name` | NCES canonical name (falls back to PDF) |
| `address` | NCES metadata |
| `coordinates` | NCES lat/long |
| `sample_id` | Extracted from PDF |
| `sample_location` | Extracted from PDF |
| `fixture_type` | Extracted from PDF (e.g. "Sink", "Water Cooler") |
| `draw_type` | `first_draw`, `flush`, or blank |
| `sample_date` | Extracted from PDF, ISO format |
| `year` | Year from date or report |
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

Prompt caching reduces repeated system prompt costs by ~90% within a batch run. SHA-256 dedup means re-running the same folder doesn't re-spend on files already in the database.

Use `--limit N` for a trial run before committing to a large folder.

---

## Tuning

Settings in `.env` (all optional — safe defaults are used):

| Setting | Default | Purpose |
|---|---|---|
| `NJLEAD_LLM_PROVIDER` | `bedrock` | `bedrock` or `anthropic` |
| `NJLEAD_BEDROCK_MODEL_ID` | `us.anthropic.claude-opus-4-6-v1` | Bedrock model for extraction |
| `NJLEAD_LLM_MAX_TOKENS` | `16384` | Max response size (tokens) |
| `NJLEAD_CONFIDENCE_THRESHOLD` | `0.70` | Rows below this go to `needs_review` |
| `NJLEAD_SCAN_DPI` | `150` | Resolution for rendering scanned pages |

---

## Project structure

```
nj_lead_pipeline_v2/
├── requirements.txt
├── pyproject.toml              <- makes `njlead` a CLI command
├── .env.example                <- credential template (copy to .env)
├── njlead/
│   ├── cli.py                  <- Typer CLI: init, ingest, check-llm, export
│   ├── config.py               <- env/.env settings (provider, model, thresholds)
│   ├── db/
│   │   ├── models.py           <- SQLAlchemy ORM (6 tables)
│   │   └── session.py          <- engine + Session factory
│   ├── ingest/
│   │   ├── loader.py           <- folder walk + per-file orchestration
│   │   ├── extractor.py        <- PyMuPDF text extraction, page-by-page
│   │   ├── page_classifier.py  <- Agent 1: Haiku page classifier
│   │   ├── schema.py           <- Pydantic extraction schema (the "blank form")
│   │   ├── llm_extractor.py    <- Agent 2: Opus extraction client
│   │   ├── verifier.py         <- Agent 3: Sonnet verification agent
│   │   ├── validation.py       <- Rule-based QA/QC gate (5 checks)
│   │   └── llm_pipeline.py     <- orchestrates classify -> extract -> verify -> validate -> write
│   ├── reference/
│   │   ├── downloader.py       <- fetches NCES CCD + EDGE files
│   │   └── matcher.py          <- fuzzy school-name matcher (rapidfuzz)
│   └── export/
│       └── writer.py           <- CSV export with NCES join
└── data/
    ├── (your PDFs here)
    └── reference/              <- cached NCES files
        ├── nj_schools_<YEAR>.csv
        └── unmatched_schools.csv
```
