"""
Text parser for NJ lead testing reports.

This module takes the raw text extracted from a PDF page and tries to find:
  - School name and district
  - Sample IDs (e.g. "S-001", "LCR-042", "24-0909-01")
  - Sample locations (e.g. "Boys Bathroom Sink 1F", "Kitchen Faucet")
  - Lead concentration in ppb (e.g. "0.5", "12.3", "ND" for non-detect)
  - Sample date or test year
  - Action level (usually 15 ppb)

The PDFs come from many different vendors and labs, so the parser uses
multiple strategies in priority order:
  1. EMSL lab format (sample blocks with "Lead" + result + "ug/L")
  2. EC Table 2 format (Sample # | Location | Lead | result ppb)
  3. LEW/EHS tabular format (Sample ID | Description | Concentration ppb)
  4. RAMM format (Sample # | Location | "None Detected" or "X.XX ppb")
  5. Bridgeton summary format (Location | First Draw ppb | Flush ppb)
  6. Loose scan for any ppb/ug/L values (fallback)

Main function: parse_page(raw_text) → ParsedPage
"""

import re
from dataclasses import dataclass, field
from datetime import date


# ---------------------------------------------------------------------------
# Output data structures
# ---------------------------------------------------------------------------

@dataclass
class ParsedMeasurement:
    """
    One water sample with its lead measurement.

    Fields that couldn't be extracted are None.
    """
    sample_id: str | None
    location: str | None
    result_ppb: float        # The lead concentration — always required
    action_level_ppb: float | None
    sample_date: date | None
    test_year: int | None
    # Fixture name extracted from the location string by a keyword heuristic.
    # None when no known fixture keyword was found.
    fixture_type: str | None = None


@dataclass
class ParsedPage:
    """
    Everything extracted from one page of a PDF.

    A page may contain zero measurements (e.g. cover pages, footnotes)
    or many (e.g. a table with 30 samples).
    """
    school_name: str | None
    district: str | None
    measurements: list[ParsedMeasurement] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Regex patterns — school / district / date / action level
# ---------------------------------------------------------------------------

# School name — looks for a line after common labels
# Example: "School: Lincoln Elementary School"
# Also matches "Facility:\nCadwalader Elementary School" (label on one line, value on next)
_SCHOOL_LABEL_RE = re.compile(
    r"(?:school|facility|building|site)\s*[:\-]\s*\n?\s*(.+)",
    re.IGNORECASE,
)

# District — looks for a line after "District:"
_DISTRICT_RE = re.compile(
    r"district\s*[:\-]\s*(.+)",
    re.IGNORECASE,
)

# Date — matches MM/DD/YYYY, MM-DD-YYYY, or YYYY-MM-DD
_DATE_RE = re.compile(
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})"   # MM/DD/YYYY or MM-DD-YYYY
    r"|(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})",  # YYYY-MM-DD
)

# Year only — a 4-digit year between 2000 and 2030
_YEAR_RE = re.compile(r"\b(20[0-3]\d)\b")

# Action level — the threshold ppb value (usually 15)
_ACTION_LEVEL_RE = re.compile(
    r"(?:action\s*level|AL)\s*[=:\-]?\s*(\d+(?:\.\d+)?)\s*(?:ppb|ug/L|µg/L|parts\s+per\s+billion)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Blacklist patterns — things that look like numbers but are NOT lead values
# ---------------------------------------------------------------------------

# These patterns appear in PDF text and get falsely matched as lead values.
# We filter them out before reporting measurements.
_BLACKLIST_RE = re.compile(
    r"EPA\s*200\.8"              # Lab method name
    r"|Route\s+\d+"             # Street address "Route 130"
    r"|NJ\s+\d{5}"             # Zip code "NJ 08077"
    r"|08\d{3}"                 # NJ zip codes starting with 08xxx
    r"|07\d{3}"                 # NJ zip codes starting with 07xxx
    r"|Page\s+\d+\s+of"        # Page numbers
    r"|\d{3}[-.)]\d{3}[-.)]\d{4}"  # Phone numbers
    r"|LIMS\s+Reference"        # Lab reference IDs
    r"|Prep\s*/\s*Analytical"   # Lab method headers
    r"|Project\s*(?:#|No|Number)" # Project number lines
    r"|Certification\s*#"       # Certification numbers
    r"|EMSL\s+Order"            # Lab order IDs
    r"|QA/QC"                   # Quality control samples
    r"|Field\s+Blank"           # Blank samples (not real measurements)
    r"|Blank\b",                # Blank samples
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result value parsing
# ---------------------------------------------------------------------------

_NON_DETECT_RE = re.compile(
    r"(?:ND|BDL|non[-\s]?detect(?:ed)?|not\s+detected|none\s+detected"
    r"|<\s*(?:MDL|RL|LOQ))\b",
    re.IGNORECASE,
)

_LESS_THAN_VALUE_RE = re.compile(r"<\s*(\d+(?:\.\d+)?)")


def _parse_result_value(value_str: str) -> float | None:
    """
    Convert a result string to a float ppb value.

    'ND', 'None Detected', etc. → 0.0 (non-detect = below threshold)
    '<1.00' → 1.0 (reported as the detection limit)
    '5.74' → 5.74
    Anything else → None
    """
    value_str = value_str.strip()

    # Non-detect: treat as 0.0 ppb
    if _NON_DETECT_RE.search(value_str):
        return 0.0

    # Less-than value: "<1.00" → 1.0 (the detection limit)
    lt_match = _LESS_THAN_VALUE_RE.search(value_str)
    if lt_match:
        try:
            return float(lt_match.group(1))
        except ValueError:
            pass

    # Plain number
    num_match = re.match(r"(\d+(?:\.\d+)?)", value_str)
    if num_match:
        try:
            return float(num_match.group(1))
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Strategy 1: EMSL lab format
# ---------------------------------------------------------------------------
# Two sub-formats exist:
#
# A) Newer (Trenton/Paterson 2025): blocks start with "Sample:" on its own line
#    Sample:
#    WF-1/1st Floor Halfway by Room #3
#    Lims Reference ID: AD17577-01  Matrix: Drinking Water
#    Sampled: 04/17/25 07:20:00
#    Metals
#    Lead    ND    1    1.00    ug/L
#
# B) Older (Bridgeton/Broad Street 2022): blocks start with "Client Sample Description"
#    Client Sample Description  Lab ID:
#    BRD-FB                     012208876-0001
#    Collected: 5/28/2022  7:45:00 AM
#    ...
#    200.8   Lead   VD   ND   ppb   ...


def _parse_emsl_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse EMSL Analytical lab report format (both old and new sub-formats).
    """
    # Check if this looks like an EMSL page at all
    if "EMSL" not in text and "Matrix: Drinking Water" not in text and "Matrix:Drinking Water" not in text:
        return []

    measurements = []

    # Try Format A first (newer "Sample:" blocks)
    measurements = _parse_emsl_new_format(text)
    if measurements:
        return measurements

    # Try Format B (older "Client Sample Description" blocks)
    measurements = _parse_emsl_old_format(text)
    return measurements


def _parse_emsl_new_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse newer EMSL format where blocks start with 'Sample:' on its own line.

    The Lead result appears as separate tokens on the same or adjacent lines:
        Lead   ND   1   1.00   ug/L
    or with newlines between them:
        Lead\n05/15/25  21:06\n1.00\n4.16\nug/L
    """
    measurements = []

    # Split on "Sample:" appearing at start of line or after whitespace
    blocks = re.split(r"(?=(?:^|\n)\s*Sample:\s*\n)", text)

    for block in blocks:
        if "Matrix: Drinking Water" not in block and "Matrix:Drinking Water" not in block:
            continue

        # Skip blank/QC samples
        if re.search(r"(?:Blank|QA/QC|Field\s+Blank)", block[:300], re.IGNORECASE):
            continue

        # Extract location: text between "Sample:\n" and "Lims Reference" or "Matrix:"
        # The newer format puts the location on the line after "Sample:"
        loc_match = re.search(
            r"Sample:\s*\n\s*"
            r"(?:(?:AD|[A-Z]{2})\d[\d\-]*\s+Matrix:\s*Drinking\s+Water\s*\n)?"
            r"(.+?)(?:\n\s*Lims\s+Reference|\n\s*Matrix:)",
            block, re.IGNORECASE | re.DOTALL,
        )
        location = None
        sample_id = None
        if loc_match:
            loc_text = loc_match.group(1).strip()
            # Clean up multi-line location text
            loc_text = re.sub(r"\s*\n\s*", " ", loc_text).strip()
            # The location often starts with "01/" or "WF-1/" — split ID from description
            id_loc = re.match(r"([\w\-]+\s*[-]?\s*[\w\-]*?)\s*[/\\]\s*(.+)", loc_text)
            if id_loc:
                sample_id = id_loc.group(1).strip()
                location = id_loc.group(2).strip()
            else:
                location = loc_text
                lab_id = re.search(r"(AD\d+[-]\d+)", block)
                if lab_id:
                    sample_id = lab_id.group(1)

        # Find Lead result — column order varies between EMSL sub-formats.
        #   Paterson: Lead\n  5.75\n  1\n  1.00\n  ug/L   (Result, DF, RL, Units)
        #   Trenton:  Lead\n  05/15/25 21:06\n  1.00\n  4.16\n  ug/L  (Date, RL, Result, Units)
        # Check if there's a date right after Lead — that determines the format.
        has_date_after_lead = re.search(
            r"Lead\s*\n?\s*\d{1,2}/\d{1,2}/\d{2,4}",
            block, re.IGNORECASE,
        )
        if has_date_after_lead:
            # Trenton format: skip date+time, skip RL, grab result
            lead_match = re.search(
                r"Lead\s*\n?\s*"
                r"\d{1,2}/\d{1,2}/\d{2,4}[\s\n]+\d{1,2}:\d{2}[\s\n]+"  # date + time
                r"\d+\.\d+[\s\n]+"                    # RL (e.g. 1.00)
                r"(ND|<?\s*\d+(?:\.\d+)?)",           # Result
                block, re.IGNORECASE,
            )
        else:
            # Paterson format: result is first token after Lead
            lead_match = re.search(
                r"Lead\s*\n?\s*"
                r"(ND|<?\s*\d+(?:\.\d+)?)",           # Result
                block, re.IGNORECASE,
            )
        if not lead_match:
            continue

        # Verify units appear somewhere after Lead in this block
        units_after_lead = re.search(
            r"Lead[\s\S]{0,120}?(ug/L|µg/L|ppb|pg/L|g/L|\u00b5g/L)",
            block, re.IGNORECASE,
        )
        if not units_after_lead:
            continue

        raw_value = lead_match.group(1).strip()
        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        # Extract sample date
        sample_date = None
        test_year = None
        sampled_match = re.search(r"Sampled:\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", block)
        if sampled_match:
            try:
                m, d, y = int(sampled_match.group(1)), int(sampled_match.group(2)), int(sampled_match.group(3))
                if y < 100:
                    y += 2000
                sample_date = date(y, m, d)
                test_year = y
            except ValueError:
                pass

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


def _parse_emsl_old_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse older EMSL format where blocks start with 'Client Sample Description'.

    Layout:
        Client Sample Description  Lab ID:
        BRD-FB                      012208876-0001
        Collected: 5/28/2022  7:45:00 AM
        Method  Parameter  Result  Units  ...
        METALS
        200.8  Lead  VD  ND  ppb  ...
    """
    measurements = []

    # Split on "Client Sample Description" headers
    blocks = re.split(r"(?=Client\s+Sample\s+Description)", text, flags=re.IGNORECASE)

    for block in blocks:
        if "Lead" not in block:
            continue

        # Skip QC/blank samples
        if re.search(r"(?:Blank|QA/QC|Field\s+Blank)", block[:300], re.IGNORECASE):
            continue

        # Extract sample ID (Lab ID) — e.g. "BRD-FB" or "BRD-SO-02-MEDIA"
        # It appears right after "Client Sample Description" and before the lab number
        id_match = re.search(
            r"(?:Lab\s+ID:|Client\s+Sample\s+Description\s*\n?\s*Lab\s+ID:)\s*\n?\s*"
            r"([\w\-]+)",
            block, re.IGNORECASE,
        )
        sample_id = id_match.group(1).strip() if id_match else None

        # Extract collection date
        sample_date = None
        test_year = None
        date_match = re.search(r"Collected:\s*\n?\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", block)
        if date_match:
            try:
                m, d, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                if y < 100:
                    y += 2000
                sample_date = date(y, m, d)
                test_year = y
            except ValueError:
                pass

        # Find Lead result — in old format it's: 200.8  Lead  VD  (result)  ppb
        # Values may be on separate lines
        lead_match = re.search(
            r"Lead\s*\n?\s*"
            r"(?:VD|[A-Z]{1,3})?\s*\n?\s*"         # optional analyst initials like "VD"
            r"(ND|<?\s*\d+(?:\.\d+)?)\s*\n?\s*"     # result value
            r"(ppb|ug/L|µg/L|\u00b5g/L)",           # units
            block, re.IGNORECASE,
        )
        if not lead_match:
            continue

        raw_value = lead_match.group(1).strip()
        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        # Use sample_id as location if we don't have a better description
        location = sample_id

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Strategy 2: EC Table 2 format (Environmental Connection / Trenton)
# ---------------------------------------------------------------------------
# Pattern:
#   01    Water Chiller Fountain in Hall by Teacher's Lounge    Lead    <1.00    Not Analyzed    15
#   The table has: Sample # | Location | Parameter | 1st Draw (ppb) | Flush (ppb) | Action Level

def _parse_ec_table_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse Environmental Connection Table 2 format.

    Rows look like:
        01    Water Chiller Fountain...    Lead    <1.00    Not Analyzed    15
        08/F08    Double Basin Sink...     Lead    18.0     58.2            15
    """
    measurements = []

    # Only try if this page looks like it has Table 2
    if "Table 2" not in text and "Analytical Results" not in text:
        return []
    if "Sample #" not in text and "Sample Location" not in text:
        return []

    # Match rows: number(s) | location text | "Lead" | result | optional flush | action level
    row_re = re.compile(
        r"^(\d{1,3}(?:/F?\d{1,3})?)\s+"     # Sample # (e.g. "01", "08/F08")
        r"(.+?)\s+"                            # Location text
        r"Lead\s+"                             # Parameter = Lead
        r"(<?\s*\d+(?:\.\d+)?|ND|Not\s+Detected)\s+"  # 1st draw result
        r"(?:Not\s+Analyzed|<?\s*\d+(?:\.\d+)?|ND)\s*" # Flush result or "Not Analyzed"
        r"(?:(\d+(?:\.\d+)?))?"                # Action level (optional)
        ,
        re.IGNORECASE | re.MULTILINE,
    )

    # Extract date from the page
    sample_date, test_year = _extract_date(text)

    for match in row_re.finditer(text):
        raw_id = match.group(1).strip()
        location = match.group(2).strip()
        raw_value = match.group(3).strip()
        al_str = match.group(4)

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        action_level = float(al_str) if al_str else 15.0

        measurements.append(ParsedMeasurement(
            sample_id=raw_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=action_level,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Strategy 3: LEW / EHS tabular format
# ---------------------------------------------------------------------------
# Pattern:
#   Date Collected | Analysis Date | Sample ID | Sample Description | Concentration (ug/L) Or ppb
#   09/17/2023     | 09/26/2023    | 1-S       | NURSES SINK       | 1.56

def _parse_lew_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse LEW Environmental / EHS lab report table format.

    These tables have columns:
        Date Collected | Analysis Date | Sample ID | Sample Description | Concentration
    or:
        Lab Sample Number | Collection Location | Collection Date | Analysis Date | ID | Concentration
    """
    measurements = []

    # Check if this looks like a LEW/EHS results page
    has_lew_header = bool(re.search(
        r"(?:Concentration|ug/L.*ppb|ppb.*ug/L|Sample\s+ID.*Description.*Concentration"
        r"|Collection\s+Location.*Concentration)",
        text, re.IGNORECASE,
    ))
    if not has_lew_header:
        return []

    # Pattern for LEW format: date | date | sample_id | description | value
    # The school name appears as a section header before the sample rows
    current_school = None

    lines = text.split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Detect school header lines (bold text between blank lines, no numbers)
        # School headers in LEW format appear as standalone lines like:
        # "Cheesequake Elementary School"
        if (re.match(r"^[A-Z][A-Za-z\s.\']+(?:School|Elementary|Middle|High|Academy|Center|ECC|HS|MS)\s*$", line, re.IGNORECASE)
                and not re.search(r"\d", line)):
            current_school = line.strip()
            continue

    # Match data rows — LEW format with dates and concentration
    # Two sub-formats:
    # A) date | date | sample_id | description | concentration
    lew_row_re = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+"      # Date Collected
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+"       # Analysis Date
        r"([\w\-/]+)\s+"                        # Sample ID
        r"(.+?)\s+"                             # Sample Description
        r"(<?\s*\d+(?:\.\d+)?)\s*$",           # Concentration
        re.MULTILINE,
    )

    for match in lew_row_re.finditer(text):
        date_str = match.group(1)
        sample_id = match.group(3).strip()
        location = match.group(4).strip()
        raw_value = match.group(5).strip()

        # Skip QA/QC samples
        if "QA/QC" in location.upper() or "BLANK" in location.upper():
            continue

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        # Parse the collection date
        sample_date = None
        test_year = None
        date_match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_str)
        if date_match:
            try:
                m, d, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                if y < 100:
                    y += 2000
                sample_date = date(y, m, d)
                test_year = y
            except ValueError:
                pass

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    # B) EHS format: concentration | lab_num | location | date | date | id
    ehs_row_re = re.compile(
        r"^(<?\d+(?:\.\d+)?)\s+"                # Concentration
        r"(\d{2}-\d{2}-\d{5}-\d{3})\s+"         # Lab Sample Number
        r"(.+?)\s+"                               # Collection Location
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+"          # Collection Date
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s+"          # Analysis Date
        r"([\w\-]+)",                              # Narrative ID
        re.MULTILINE,
    )

    for match in ehs_row_re.finditer(text):
        raw_value = match.group(1).strip()
        location = match.group(3).strip()
        date_str = match.group(4)
        sample_id = match.group(6).strip()

        if "QA/QC" in location.upper() or "BLANK" in location.upper():
            continue

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        sample_date = None
        test_year = None
        date_match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", date_str)
        if date_match:
            try:
                m, d, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                if y < 100:
                    y += 2000
                sample_date = date(y, m, d)
                test_year = y
            except ValueError:
                pass

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Strategy 4: RAMM format
# ---------------------------------------------------------------------------
# Pattern:
#   24-0909-01   Hallway Adjacent Main Office (Water Fountain on Left)   None Detected
#   24-0909-04   Media Center (Sink)                                     4.64 ppb

def _parse_ramm_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse RAMM Environmental format.

    Rows: Sample# | Location | "None Detected" or "X.XX ppb"
    """
    measurements = []

    # Only try if this page looks like a RAMM report
    if "RAMM" not in text and "WATER QUALITY MONITORING RESULTS" not in text:
        return []

    row_re = re.compile(
        r"^(\d{2}-\d{3,4}-\d{2})\s+"        # Sample # like "24-0909-01" (strict format)
        r"(.+?)\s{2,}"                        # Location (at least 2 spaces before result)
        r"(None\s+Detected|\d+(?:\.\d+)?)\s*(?:ppb)?",  # Result
        re.IGNORECASE | re.MULTILINE,
    )

    sample_date, test_year = _extract_date(text)

    for match in row_re.finditer(text):
        sample_id = match.group(1).strip()
        location = match.group(2).strip()
        raw_value = match.group(3).strip()

        # Skip blanks
        if "blank" in location.lower() or "field blank" in location.lower():
            continue

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Strategy 5: Bridgeton summary format
# ---------------------------------------------------------------------------
# Pattern:
#   Admin Building    First Draw (ppb)   30-sec Flush (ppb)
#   Sink, Asst Super Office    24.5    2.10

def _parse_bridgeton_summary_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse the Bridgeton BOE summary table format.

    These have school headers followed by:
        Location | First Draw (ppb) | Flush (ppb)
    """
    measurements = []

    # Only try if this looks like a Bridgeton summary
    if "First Draw (ppb)" not in text and "First Draw(ppb)" not in text:
        return []

    sample_date, test_year = _extract_date(text)

    # Match lines that have a location followed by a numeric ppb value.
    # The tricky part: room numbers like "Rm 123" look like lead values.
    # We require the value to have a decimal point OR be followed by
    # whitespace + another number (the flush value) or end of line.
    # Also, values >500 are almost certainly not lead measurements.
    row_re = re.compile(
        r"^((?:Sink|Bubbler|Water\s+Cooler|Water\s+Fountain|Bottle\s+Filler|Faucet|Spigot)"
        r"[^0-9\n]*?(?:\s+(?:Rm|Room)\s+\w+)?[^0-9\n]*?)"   # Location with possible room number
        r"\s{2,}"                                # At least 2 spaces separate location from value
        r"(<?\s*\d+(?:\.\d+))\s*",              # Value MUST have decimal point
        re.IGNORECASE | re.MULTILINE,
    )

    for match in row_re.finditer(text):
        location = match.group(1).strip()
        # Clean up extra whitespace and special chars from location
        location = re.sub(r"\s+", " ", location).strip(" \u2013\u2014-–—")
        raw_value = match.group(2).strip()

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        measurements.append(ParsedMeasurement(
            sample_id=None,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Strategy 6: TTI inline results — "(FAIL. RESULT XX)" or "(RESULT ND)"
# ---------------------------------------------------------------------------

def _parse_tti_inline_format(text: str) -> list[ParsedMeasurement]:
    """
    Parse TTI Environmental summary format with inline results.

    Lines like:
        W-AHWF-7 (FAIL. RESULT 218)
        CR-6-DF/Room 5 - Sink with Fountain (FAIL. RESULT 63.7)
        CS1/Nurse's Office – Sink (RESULT ND)
    """
    measurements = []

    result_re = re.compile(
        r"([\w\-/]+(?:/[^()\n]+)?)\s*"          # Location/ID
        r"\((?:FAIL\.?\s*)?RESULT\s+"            # "(FAIL. RESULT" or "(RESULT"
        r"(ND|\d+(?:\.\d+)?)\s*\)",              # Value or ND
        re.IGNORECASE,
    )

    sample_date, test_year = _extract_date(text)

    for match in result_re.finditer(text):
        loc_text = match.group(1).strip()
        raw_value = match.group(2).strip()

        result_ppb = _parse_result_value(raw_value)
        if result_ppb is None:
            continue

        # Split location ID from description
        sample_id = None
        location = loc_text
        id_loc = re.match(r"([\w\-]+)[/\\](.+)", loc_text)
        if id_loc:
            sample_id = id_loc.group(1).strip()
            location = id_loc.group(2).strip()

        measurements.append(ParsedMeasurement(
            sample_id=sample_id,
            location=location,
            result_ppb=result_ppb,
            action_level_ppb=15.0,
            sample_date=sample_date,
            test_year=test_year,
        ))

    return measurements


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _extract_school(text: str) -> str | None:
    """
    Find school name using multiple strategies.

    Checks for:
    1. Labels like "School:", "Facility:", "Building:"
    2. "Performed At:" (LEW format)
    3. Project name lines in EMSL headers
    """
    # Strategy 1: standard label
    match = _SCHOOL_LABEL_RE.search(text)
    if match:
        name = match.group(1).strip()
        # Reject if it looks like an address or generic text
        if len(name) > 3 and not re.match(r"^\d+\s", name):
            return name

    # Strategy 2: "Performed At:" (LEW format)
    match = re.search(r"Performed\s+At:\s*\n?\s*(.+)", text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        if len(name) > 3 and not re.match(r"^\d+\s", name):
            return name

    # Strategy 3: EMSL "client designated project:" followed by school name
    match = re.search(
        r"client\s+designated\s+project:\s*\n?\s*(?:Project\s+ID:\s*\w+\s*\n?\s*)?(.+)",
        text, re.IGNORECASE,
    )
    if match:
        name = match.group(1).strip()
        if len(name) > 3 and not re.match(r"^\d+\s", name):
            return name

    # Strategy 4: EMSL header — project number / school name pattern
    # e.g. "25156-01 / Cadwalader Elementary School -"
    match = re.search(
        r"\d{2,5}[-/]?\d*\s*/\s*(.+?)(?:\s*-\s*\d|\s*$)",
        text, re.IGNORECASE | re.MULTILINE,
    )
    if match:
        name = match.group(1).strip()
        # Must look like a school name — at least 5 chars, has letters, no phone/date patterns
        if (len(name) > 5
                and re.search(r"[A-Za-z]{3,}", name)
                and not re.search(r"\d{3}[-.)]\d{3}", name)):  # not a phone number
            return name

    return None


def _extract_district(text: str) -> str | None:
    """Find district name from labels or header patterns."""
    # Standard "District:" label
    match = _DISTRICT_RE.search(text)
    if match:
        name = match.group(1).strip()
        # Filter out things that are clearly not district names
        if "Lead" not in name and "Water" not in name and len(name) > 3:
            return name

    # "Performed For:" (LEW format)
    match = re.search(r"Performed\s+For:\s*\n?\s*(.+)", text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        if len(name) > 3 and "LLC" not in name:
            return name

    return None


def _extract_date(text: str) -> tuple[date | None, int | None]:
    """
    Try to find a full date, falling back to year only.

    Returns (date_object_or_None, year_int_or_None).
    """
    # Try full date first — look for dates near keywords like "sampled", "collected", "performed"
    # to avoid matching random dates in addresses or headers
    date_context_re = re.compile(
        r"(?:sampl(?:ed|ing)|collect(?:ed|ion)|performed|tested|date)\s*[:\-]?\s*"
        r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})",
        re.IGNORECASE,
    )
    match = date_context_re.search(text)
    if match:
        try:
            m, d, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if y < 100:
                y += 2000
            return date(y, m, d), y
        except ValueError:
            pass

    # Generic date match
    match = _DATE_RE.search(text)
    if match:
        try:
            if match.group(1):  # MM/DD/YYYY
                month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            else:               # YYYY-MM-DD
                year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
            return date(year, month, day), year
        except ValueError:
            pass

    # Fall back to year only
    match = _YEAR_RE.search(text)
    if match:
        return None, int(match.group(1))

    return None, None


def _extract_action_level(text: str) -> float | None:
    """Find the action level threshold (usually 15 ppb)."""
    match = _ACTION_LEVEL_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Fixture / location split
# ---------------------------------------------------------------------------
#
# The PDFs report location as a single free-text string that combines a place
# (e.g. "1st Floor Teachers Lounge") with a fixture (e.g. "Water Chiller").
# The gold-standard CSV expects these as two separate columns, so we run a
# keyword-based splitter on every parsed measurement.
#
# The keyword list is ordered longest-first so multi-word fixtures like
# "Water Chiller Fountain" win over the shorter "Water Chiller" they contain.

# Each entry is the canonical name we want in the output column.
# The splitter matches case-insensitively and treats whitespace flexibly.
_FIXTURE_KEYWORDS: tuple[str, ...] = (
    # Multi-word, longest first so they win the regex alternation
    "Water Chiller Fountain",
    "Water Chiller",
    "Bottle Filler",
    "Bottle Fill",
    "Sink with Fountain",
    "Triple Basin Sink",
    "Double Basin Sink",
    "Food Prep Sink",
    "Sink Faucet",
    "Coffee Pot",
    "Ice Machine",
    "Dishwasher",
    "Water Fountain",
    "Drinking Fountain",
    "Kettle",
    "Spigot",
    "Faucet",
    "Sink",
)

# Build one regex that finds any fixture keyword as a whole-word match.
# `re.IGNORECASE` so "WATER CHILLER" and "water chiller" both match.
_FIXTURE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _FIXTURE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _split_location_fixture(loc: str | None) -> tuple[str | None, str | None]:
    """
    Split a raw location string into (sample_location, fixture_type).

    Strategy: search for the FIRST fixture keyword in the string. The fixture
    portion gets normalized to title case. Whatever text is left over (after
    removing the fixture and tidying punctuation) becomes the sample_location.

    If no keyword matches we keep the original string as sample_location and
    leave fixture_type as None — the row still ships, the gap is just visible.

    Examples:
      "01/1st Floor Teachers Lounge, Water Chiller Fountain"
        -> ("01/1st Floor Teachers Lounge", "Water Chiller Fountain")
      "Kitchen, Triple Basin Sink"
        -> ("Kitchen", "Triple Basin Sink")
      "Water Chiller Fountain in Hall by Teacher's Lounge"
        -> ("in Hall by Teacher's Lounge", "Water Chiller Fountain")
      "Cover letter only"
        -> ("Cover letter only", None)
    """
    if not loc:
        return loc, None

    match = _FIXTURE_RE.search(loc)
    if not match:
        return loc.strip() or None, None

    # The matched fixture, title-cased so output is consistent.
    fixture = match.group(1).title()

    # Remove the fixture span from the original; tidy up surrounding punctuation
    # (commas, dashes, parentheses, leftover whitespace).
    remaining = loc[: match.start()] + loc[match.end():]
    remaining = re.sub(r"\s*[,\-/–—]\s*", " ", remaining)   # punctuation -> space
    remaining = re.sub(r"\s+", " ", remaining).strip(" ()-,")
    sample_location = remaining if remaining else None

    return sample_location, fixture


# ---------------------------------------------------------------------------
# Main parsing function
# ---------------------------------------------------------------------------

def parse_page(raw_text: str) -> ParsedPage:
    """
    Parse the raw text from one PDF page and extract lead testing data.

    Tries multiple format-specific strategies in priority order.
    The first strategy that finds measurements wins — this prevents
    double-counting when the same page matches multiple patterns.

    Returns a ParsedPage even if nothing is found (empty measurements list).
    """
    result = ParsedPage(
        school_name=_extract_school(raw_text),
        district=_extract_district(raw_text),
    )

    # Try each strategy in order. Stop at the first one that finds data.
    # This ordering matters:
    #   1. EMSL is most specific (structured lab blocks)
    #   2. EC Table 2 is structured with "Lead" column
    #   3. LEW/EHS has dated columns
    #   4. RAMM has "None Detected" / "ppb" pattern
    #   5. Bridgeton summary has "First Draw (ppb)" header
    #   6. TTI inline has "(RESULT XX)" markers
    strategies = [
        _parse_emsl_format,
        _parse_ec_table_format,
        _parse_lew_format,
        _parse_ramm_format,
        _parse_bridgeton_summary_format,
        _parse_tti_inline_format,
    ]

    for strategy in strategies:
        measurements = strategy(raw_text)
        if measurements:
            # Run the fixture/location splitter once on every measurement so the
            # 6 strategy functions don't each have to know about fixture splitting.
            for m in measurements:
                sample_loc, fixture = _split_location_fixture(m.location)
                m.location = sample_loc
                m.fixture_type = fixture
            result.measurements = measurements
            return result

    return result
