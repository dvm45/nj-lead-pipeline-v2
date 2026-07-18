# NJ Lead Pipeline V2

Extracts structured data from NJ school drinking water lead testing PDFs and stores it in a local SQLite database.

Each PDF is read by Claude (Anthropic's LLM) in a single API call. The model returns a structured record that's validated locally before anything is written to the database. Scanned PDFs and unfamiliar lab formats are handled the same way as clean typed reports — the model reads them directly.

---

## What it does

1. **`njlead init`** — creates the database (run once before anything else).
2. **`njlead ingest <folder>`** — finds every PDF in a folder, sends each one to Claude, and writes the validated results to `leads.db`.
3. **`njlead refresh-reference`** — downloads NCES school metadata (county, address, coordinates) for every NJ public school. Run once before your first export, then yearly.
4. **`njlead export`** — joins the ingested PDF data with the NCES metadata and writes everything to a CSV file you can open in Excel.

---

## Setup (Windows)

### Step 1 — Make sure Python 3.11 or newer is installed

Open a terminal (Windows Terminal or Command Prompt) and run:

```
python --version
```

You should see `Python 3.11.x` or higher. If not, download Python from [python.org](https://www.python.org/downloads/) and check "Add Python to PATH" during install.

---

### Step 2 — Open a terminal in the project folder

Right-click the `nj_lead_pipeline_v2` folder → "Open in Terminal"
*(or use `cd` to navigate there)*

---

### Step 3 — Create a virtual environment

A virtual environment keeps this project's dependencies separate from other Python projects.

```
python -m venv .venv
```

Then activate it:

```
.venv\Scripts\activate
```

Your terminal prompt should change to show `(.venv)` at the start. **You need to do this every time you open a new terminal.**

---

### Step 4 — Install dependencies

```
pip install -r requirements.txt
pip install -e .
```

The first command installs the libraries. The second makes the `njlead` command available.

---

### Step 5 — Set your Anthropic API key

The pipeline calls the Claude API to read each PDF, so you need an API key.

1. Get a key at [console.anthropic.com](https://console.anthropic.com/) → API Keys.
2. Copy `.env.example` to `.env`:
   ```
   copy .env.example .env
   ```
3. Open `.env` in a text editor and paste your key after `ANTHROPIC_API_KEY=`.
4. Save and close the file.

The `.env` file is gitignored — the key stays on your machine only.

---

### Step 6 — Verify installation

```
njlead --help
```

You should see the help text listing `init`, `ingest`, `check-llm`, `refresh-reference`, and `export`.

Then confirm your key is picked up:

```
njlead check-llm
```

This prints a status report without spending anything. You want to see `API key set: yes` and `anthropic SDK installed: yes`.

---

## Usage

### Initialize the database (first time only)

```
njlead init
```

This creates `leads.db` in the current folder. You only need to do this once.

---

### Ingest PDFs

Put your PDF files in a folder (e.g. `data/`) and start with a small trial run to check things work before spending real money:

```
njlead ingest data/ --limit 3
```

Then, once you're happy, run the full folder:

```
njlead ingest data/
```

For each PDF, the pipeline will:
- Skip the file if it's already in the database (matched by SHA-256 hash)
- Extract text from each page with PyMuPDF (kept in the `pages` table for reference)
- Send the whole report to Claude in one API call
- Validate every returned measurement (unit check, range check, source-span check, confidence threshold)
- Write measurements that pass the validation gate; log the rest to the `issues` table as `needs_review`

Output example:

```
Ingesting PDFs from: data/

Found 15 PDF file(s) in data/
  [1/15] report_2019_lincoln.pdf ... ingested
  [2/15] report_2019_washington.pdf ... ingested
  [3/15] scan_2018_blurry.pdf ... ingested
  ...

----------------------------------------
  Total found:  15
  Ingested:     13
  Skipped:      1  (already in database)
  Failed:       1
----------------------------------------
```

---

### Refresh NCES reference data (one-time, then yearly)

```
njlead refresh-reference
```

The PDFs themselves don't carry county, street address, or GPS coordinates.
This command downloads two files from the National Center for Education
Statistics (NCES) and joins them into a local lookup table:

- The CCD School Directory — official school name, district, address, county for every public school in the US.
- The EDGE Geocodes file — latitude and longitude for each school.

Both files are filtered to New Jersey rows and merged into:

```
data/reference/nj_schools_<YEAR>.csv
```

This download is **the only step besides ingest that needs internet**. After it
completes, `njlead export` works fully offline. Re-run this command once a
year, or whenever NCES publishes a new school year.

If a download fails because NCES has published a newer file, edit the URL
constants at the top of `njlead/reference/downloader.py` (instructions in
that file).

---

### Export to CSV

```
njlead export
```

Writes a file like `export_2024-03-15_142301.csv` to the current folder.
Open it in Excel — each row is one lead measurement, enriched with school
metadata from NCES.

Columns: `county`, `district`, `school_name`, `address`, `coordinates`, `sample_id`, `sample_location`, `fixture_type`, `sample_date`, `lead_concentration_ppb`

To write to a specific folder:
```
njlead export --output-dir C:\Users\you\Desktop
```

**About unmatched schools:** if a school name in your PDFs can't be matched
to NCES (for example, district admin buildings that aren't actual schools),
the row still ships with the parsed sample data — but `county`, `address`,
and `coordinates` will be blank. Those schools are listed in
`data/reference/unmatched_schools.csv` so you can review them manually.

---

## Debugging tools

These are Claude Code slash commands — type them in the Claude Code chat:

| Command | What it shows |
|---|---|
| `/db-summary` | Row counts for all tables, issue breakdown |
| `/inspect-issues` | All extraction problems grouped by type |
| `/show-sample report.pdf 0` | Raw extracted text from page 0 of a file |

---

## Understanding extraction issues

The pipeline never crashes on a bad file — it logs problems to the `issues` table and moves on. Common issue types:

| Issue type | What it means |
|---|---|
| `needs_review` | A row failed the validation gate (bad unit, out-of-range value, low confidence, or the value couldn't be found in the source text). Included row + the specific reasons. |
| `no_school_found` | The model didn't return a school name for this report. |
| `no_measurements` | The model returned zero measurements — the file may be a cover letter, notification, or otherwise not a results report. |
| `llm_error` | The API call or response parsing failed. Usually transient — try again. |
| `extraction_failed` | PDF couldn't be opened (corrupt, password-protected, wrong format). |

Use `/show-sample <filename> <page>` to see exactly what text was extracted from a problem page — helpful when a `needs_review` reason says the value couldn't be located in the source.

---

## Cost

- Roughly **pennies per report** on Claude Sonnet.
- A full statewide run (~800 reports) is on the order of tens of dollars, one-time.
- The SHA-256 dedup at the top of ingest means re-running the same folder does not re-spend on files already in the database.

Use `--limit N` for a small trial run before committing to a big folder.

---

## Project structure

```
nj_lead_pipeline_v2/
├── requirements.txt         ← Python dependencies
├── pyproject.toml           ← makes `njlead` a CLI command
├── .env.example             ← template for ANTHROPIC_API_KEY (copy to .env)
├── njlead/
│   ├── cli.py               ← init, ingest, check-llm, refresh-reference, export
│   ├── config.py            ← reads ANTHROPIC_API_KEY and model settings from .env
│   ├── db/
│   │   ├── models.py        ← database table definitions
│   │   └── session.py       ← database connection
│   ├── ingest/
│   │   ├── extractor.py     ← PDF text extraction (PyMuPDF)
│   │   ├── schema.py        ← the "blank form" the model fills in (Pydantic)
│   │   ├── validation.py    ← QA/QC gate — every row must clear this to be stored
│   │   ├── llm_extractor.py ← one Claude API call per report
│   │   ├── llm_pipeline.py  ← extract → validate → write for one report
│   │   └── loader.py        ← folder walk + orchestration
│   ├── reference/           ← NCES school metadata enrichment
│   │   ├── downloader.py    ← fetches CCD + EDGE files from nces.ed.gov
│   │   └── matcher.py       ← fuzzy-matches PDF school names to NCES records
│   └── export/
│       └── writer.py        ← CSV export (with NCES join)
└── data/
    ├── (put your PDFs here)
    └── reference/           ← cached NCES files (created by refresh-reference)
        ├── nj_schools_<YEAR>.csv      ← unified lookup
        └── unmatched_schools.csv      ← schools that didn't match NCES
```

---

## Tuning the extraction

The behavior of the ingest pipeline is controlled by a handful of settings in
`.env` (all optional — safe defaults are used if they're not set):

| Setting | Default | Purpose |
|---|---|---|
| `NJLEAD_LLM_MODEL` | `claude-sonnet-4-6` | Which Claude model to call |
| `NJLEAD_LLM_MAX_TOKENS` | `4096` | Max size of the model's response |
| `NJLEAD_CONFIDENCE_THRESHOLD` | `0.70` | Rows below this per-row confidence go to `needs_review` instead of being written |
| `NJLEAD_SCAN_DPI` | `150` | Resolution for rendering scanned pages to images before sending them to the model |

Raise the threshold to be stricter (more rows held for review, fewer written).
Lower it to trust the model more. Every held row is preserved in the `issues`
table with the reasons, so nothing is silently dropped either way.
