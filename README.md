# NJ Lead Pipeline V2

Extracts structured data from NJ school drinking water lead testing PDFs and stores it in a local SQLite database.

---

## What it does

1. **`njlead init`** — creates the database (run once before anything else).
2. **`njlead ingest <folder>`** — finds every PDF in a folder, extracts text with PyMuPDF, and parses out school names, sample IDs, locations, lead concentrations (ppb), and dates.
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

### Step 5 — Verify installation

```
njlead --help
```

You should see the help text listing `init`, `ingest`, `refresh-reference`, and `export`.

---

## Usage

### Initialize the database (first time only)

```
njlead init
```

This creates `leads.db` in the current folder. You only need to do this once.

---

### Ingest PDFs

Put your PDF files in a folder (e.g. `data/`) and run:

```
njlead ingest data/
```

The pipeline will:
- Find all PDF files (including in subfolders)
- Skip any file already in the database
- Extract text page by page
- Parse school names, sample IDs, locations, lead values, and dates
- Log anything it couldn't parse to the `issues` table

Output example:
```
Found 15 PDF file(s) in data/
  [1/15] report_2019_lincoln.pdf ... ingested
  [2/15] report_2019_washington.pdf ... ingested
  [3/15] scan_2018_blurry.pdf ... ingested
  ...

────────────────────────────────────────
  Total found:  15
  Ingested:     13
  Skipped:      1  (already in database)
  Failed:       1
────────────────────────────────────────
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

This download is **the only step that needs internet**. After it completes,
`njlead export` works fully offline. Re-run this command once a year, or
whenever NCES publishes a new school year.

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
| `/inspect-issues` | All parse failures grouped by type |
| `/show-sample report.pdf 0` | Raw extracted text from page 0 of a file |

---

## Understanding parse failures

The pipeline never crashes on a bad file — it logs problems to the `issues` table and moves on. Common issue types:

| Issue type | What it means |
|---|---|
| `ocr_needed` | Page had no text (scanned image). Data can't be extracted without OCR. |
| `no_school_found` | Couldn't identify school name from first page text. |
| `no_measurements` | No lead ppb values were found anywhere in the file. |
| `extraction_failed` | PDF couldn't be opened (corrupt, password-protected, wrong format). |
| `parse_error` | An unexpected error occurred while parsing a page. |

Use `/show-sample <filename> <page>` to see exactly what text was extracted from a problem page — this helps you understand why the parser didn't find what you expected, and how to improve the patterns in `njlead/ingest/parser.py`.

---

## Project structure

```
nj_lead_pipeline_v2/
├── requirements.txt         ← Python dependencies
├── pyproject.toml           ← makes `njlead` a CLI command
├── njlead/
│   ├── cli.py               ← init, ingest, refresh-reference, export commands
│   ├── db/
│   │   ├── models.py        ← database table definitions
│   │   └── session.py       ← database connection
│   ├── ingest/
│   │   ├── extractor.py     ← PDF text extraction (PyMuPDF)
│   │   ├── parser.py        ← regex parsing → structured data + fixture splitter
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

## Improving the parser

The parser in `njlead/ingest/parser.py` uses regex patterns to find data in PDF text. Because NJ reports come from many different vendors and years, no single set of patterns works for everything.

When you run `/inspect-issues` and see `no_measurements` for a file, use `/show-sample <filename> 0` to look at the raw text. Then check whether the existing regex patterns in `parser.py` should match it — often a small tweak (a new label keyword, a different spacing pattern) is all that's needed.

---

## Adding OCR support (future)

For scanned PDFs (flagged as `ocr_needed`), text extraction requires OCR.

When you're ready to add it:
1. Install [Tesseract for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
2. `pip install pytesseract pillow`
3. Add an OCR fallback in `extractor.py` that runs when `page.get_text()` returns empty

This is intentionally left out of the MVP — get the text-based PDFs working first.
