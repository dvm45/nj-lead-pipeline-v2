"""
School metadata reference data.

The PDFs the pipeline ingests don't carry county, address, or coordinates —
those have to be sourced separately. This package handles that:

  downloader.py — fetches NCES Common Core of Data files (school directory
                  + EDGE geocodes), filters to NJ, joins them into a single
                  lookup CSV stored at data/reference/nj_schools_<YEAR>.csv
  matcher.py    — loads the lookup CSV and exposes a fuzzy-match function
                  that resolves a (school_name, district) pair from a PDF
                  to its canonical NCES record

The reference data is downloaded once (via `njlead refresh-reference`) and
cached on disk. Every export reads from the cached file — no network calls
are made at export time.
"""

# The NCES file year we currently target. Update this constant when a new
# CCD release is published (typically each summer for the prior school year).
# 2024 = the 2023-24 school year file.
LATEST_REFERENCE_YEAR = 2024
