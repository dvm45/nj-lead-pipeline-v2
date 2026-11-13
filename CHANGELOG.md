# Changelog

## 1.1.0 — 2026-11-13

Major architecture update: Drive becomes input-only, all outputs are local, persistent database with dedup, and several extraction fixes from a 20-PDF test run.

### Breaking changes

- **Drive is input-only**: removed all Drive writes (upload, move-to-processed, merge). The `GDRIVE_OUTPUT_FOLDER_ID` env var is no longer used. Drive scope changed to read-only.
- **Persistent leads.db**: the database persists across runs; files already ingested (by SHA-256 hash) are skipped. `--full-refresh` backs up the DB before starting fresh. This replaces the previous fresh-DB-per-run design.
- **`export_to_csv()` returns a tuple**: `(Path, int)` — the CSV path and number of duplicate rows removed. Callers that only expected a `Path` will need updating.

### New features

- **`njlead find-duplicates`**: detects duplicate PDFs by MD5 checksum, text similarity (≥90% via rapidfuzz), and database row overlap (≥80%). Writes `output/duplicates_report.csv` and `exclude.txt`. No Bedrock calls.
- **`exclude.txt` support**: `njlead ingest` and `njlead run` skip files listed in `exclude.txt`.
- **Token usage tracking**: per-model (Haiku/Opus/Sonnet) token counts and cost estimate written to `output/last_run.txt`.
- **Year fallback chain**: report year is resolved via LLM → max(sample dates) → filename parse → folder path. New `year_source` column in CSV tracks which source was used.
- **MD5 checksum download skip**: Drive downloads compare local file MD5 with Drive's `md5Checksum` field to skip re-downloading cached files.

### Fixes

- **County 88% blank**: switched from CCD's blank `NMCNTY` column to EDGE geocode file's `NMCNTY` column. Run `njlead refresh-reference` to update.
- **Verifier dropping whole files**: removed all confidence downgrading from verifier. Low-confidence reports and flagged rows are logged to the issues table but kept in the master CSV at their original confidence.
- **Duplicate rows in output**: export now deduplicates on (school_name, sample_id, sample_location, draw_type, sample_date, lead_concentration_ppb). Batch merge also deduplicates during extraction.
- **Year missing**: the fallback chain resolves years that the LLM didn't extract.
- **Year parser false positive**: bare 2-digit year regex no longer matches lab project numbers like "24-337".

### Cost optimization

- Increased text batch size from 2 to 8 pages, reducing Opus API calls.
- Extractor model is configurable via `NJLEAD_BEDROCK_MODEL_ID` (switch to Sonnet to cut cost).

### Tests

- 53 tests (up from 32), all passing, ruff clean.
- New test files: `test_year_parser.py` (12), `test_verifier.py` (4), `test_duplicates.py` (3), `test_token_tracker.py` (4).
- Removed `test_merge.py` (merge logic no longer exists).
- Updated `test_drive.py` (read-only scope, MD5 fields) and `test_export.py` (17 columns, year_source, dedup).

## 1.0.0 — 2026-11-12

First stable release. The pipeline runs locally on Windows with Google Drive integration — no web UI, no EC2.

### What changed

- **Removed hosted pieces**: deleted `webapp/` and `deploy/`. The pipeline is now a CLI tool (`njlead`) that runs on each person's laptop.
- **Google Drive workflow** (`njlead run`): downloads PDFs from a shared Drive input folder, ingests them, exports to CSV, merges into `master.csv` on Drive, and moves processed PDFs to a `Processed` subfolder. Supports `--full-refresh`, `--limit N`, and `--local <folder>` flags.
- **3-agent LLM pipeline**: Haiku 4.5 page classifier → Opus 4.6 extractor → Sonnet 4.6 verifier. Replaced all regex parsers.
- **Output fixes**: added `school_address` (PDF fallback when NCES unmatched), `report_year`, lead-only analyte filter, `pb`→`lead` normalization. Drive subfolder structure is preserved for county fallback.
- **Validation gate**: 5 rule-based checks (structure, range, unit, source-span, confidence) on every extracted row.
- **Logging**: replaced all `print()` with the `logging` module. Per-run log files in `logs/`.
- **Haiku cost fix**: moved classify prompt to system (cacheable), lowered DPI to 72, skip classification for all-scanned reports. Spend back to ~$0.001/page.
- **Tests**: 32 tests covering validation, export, merge logic, and Drive integration (mocked).
- **Windows launchers**: `setup.bat` and `run.bat` for non-technical users.
- **Linting**: added ruff config, all checks pass.
- **Pinned dependencies**: `requirements.txt` is now a lock file from a working install.
