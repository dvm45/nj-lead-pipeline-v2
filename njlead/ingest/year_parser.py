"""
Parse a year from PDF filenames and folder paths.

Handles many naming conventions found in the NJ lead testing corpus:
  - 4-digit years: "Bridgeton ... 6-2022.pdf" → 2022
  - 2-digit years: "wanaque24.pdf" → 2024
  - School-year ranges: "21-22 Lead Testing.pdf" → 2022 (later year)
  - Date-like patterns: "Maywood_..._1-21-25.pdf" → 2025
  - Short filenames: "westmilford2025-3.pdf" → 2025, "Wayne Hills 2024.pdf" → 2024

Avoids false positives from:
  - Lab project numbers: "24-337", "25156-01" (3+ digit second part)
  - Generic numbers in school names: "PS 10", "School #42"
"""

from __future__ import annotations

import re
from pathlib import Path

# 4-digit year (2018–2030) — most reliable signal
_YEAR_4D = re.compile(r"(?<!\d)(20(?:1[89]|2\d|30))(?!\d)")

# School-year range: "21-22", "24-25" — two 2-digit numbers 1 apart
# Must NOT be followed by another digit (to exclude "24-337")
_SCHOOL_YEAR_RANGE = re.compile(
    r"(?<!\d)(\d{2})[-–](\d{2})(?!\d)"
)

# Date patterns: "1-21-25", "6-2022" — month-day-year or month-year
_DATE_MDY = re.compile(
    r"(?<!\d)(\d{1,2})[-–](\d{1,2})[-–](\d{2,4})(?!\d)"
)
_DATE_MY = re.compile(
    r"(?<!\d)(\d{1,2})[-–](20(?:1[89]|2\d|30))(?!\d)"
)

# Bare 2-digit year at end of stem or before a non-alphanumeric separator
# (e.g. "wanaque24.pdf"). Must be preceded by a letter (not underscore or
# digit) and not followed by more digits or a dash+digits (to avoid
# matching project numbers like "Report_24-337").
_YEAR_2D = re.compile(
    r"(?<=[a-zA-Z])(\d{2})(?=$|[^0-9a-zA-Z\-])"
)


def _expand_2digit(y: int) -> int | None:
    """Convert a 2-digit year to 4-digit. Returns None if out of range."""
    if 18 <= y <= 30:
        return 2000 + y
    return None


def parse_year_from_filename(filename: str) -> int | None:
    """
    Extract a year from a PDF filename.

    Returns the most likely year, or None if no year pattern is found.
    Prefers 4-digit years over ranges over 2-digit years.
    """
    stem = Path(filename).stem

    # Priority 1: explicit 4-digit year
    matches_4d = _YEAR_4D.findall(stem)
    if matches_4d:
        # Take the last one (usually the year, not a school number)
        return int(matches_4d[-1])

    # Priority 2: date pattern like "1-21-25" → year part
    for m in _DATE_MDY.finditer(stem):
        month, day, year_part = int(m.group(1)), int(m.group(2)), m.group(3)
        if len(year_part) == 4:
            y = int(year_part)
            if 2018 <= y <= 2030:
                return y
        elif len(year_part) == 2:
            y = _expand_2digit(int(year_part))
            # Validate month/day to avoid matching project numbers
            if y and 1 <= month <= 12 and 1 <= day <= 31:
                return y

    # Priority 3: month-year like "6-2022"
    for m in _DATE_MY.finditer(stem):
        return int(m.group(2))

    # Priority 4: school-year range like "21-22" or "24-25"
    for m in _SCHOOL_YEAR_RANGE.finditer(stem):
        a, b = int(m.group(1)), int(m.group(2))
        # Must be consecutive years (21-22, 24-25) not a project number (24-33)
        if b == a + 1 or (a == 99 and b == 0):
            return _expand_2digit(b)

    # Priority 5: bare 2-digit year like "wanaque24"
    for m in _YEAR_2D.finditer(stem):
        y = _expand_2digit(int(m.group(1)))
        if y:
            return y

    return None


def parse_year_from_path(file_path: Path) -> int | None:
    """
    Extract a year from folder names in a file path.

    Looks for patterns like "Newark/22" or "2024" in parent folders.
    """
    for part in file_path.parts:
        # 4-digit year in folder name
        m = _YEAR_4D.search(part)
        if m:
            return int(m.group(1))

        # 2-digit year as entire folder name (e.g. "22")
        if re.fullmatch(r"\d{2}", part):
            y = _expand_2digit(int(part))
            if y:
                return y

    return None
