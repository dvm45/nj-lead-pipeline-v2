# Changelog

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
