"""
Fuzzy school-name matcher.

Loads the lookup CSV produced by downloader.build_lookup, then exposes
SchoolMatcher.match(name, district) -> SchoolMatch | None.

Matching strategy (block-then-match for accuracy):
  1. Normalize the input — uppercase, strip filler words ("school",
     "elementary", "e.s.", etc.), collapse whitespace.
  2. Filter the candidate pool to records in the same district (also
     fuzzy-matched, threshold 80). This is the "block" step — it stops
     a "Lincoln Elementary" in Newark from matching one in Trenton.
  3. Within the blocked pool, fuzzy-match the school name (threshold 85).
  4. Return the winner with its rapidfuzz score, or None if no candidate
     clears the threshold.

The matcher caches results by (name, district) so repeated lookups during
an export pass are free.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# rapidfuzz is the modern, fast successor to fuzzywuzzy/thefuzz.
# Its `process.extractOne` returns (match_value, score, index) — much faster
# than running individual ratio() calls in a Python loop.
from rapidfuzz import fuzz, process


# -----------------------------------------------------------------------------
# Public dataclasses
# -----------------------------------------------------------------------------

@dataclass
class SchoolMatch:
    """A successful match against the NCES reference lookup."""
    nces_id: str
    school_name: str        # NCES canonical name
    district_name: str
    county: str
    address: str
    latitude: float | None
    longitude: float | None
    confidence: float       # rapidfuzz score, 0-100


# -----------------------------------------------------------------------------
# Name normalization
# -----------------------------------------------------------------------------

# Words to strip before fuzzy comparison — they're noise that inflates
# similarity scores between unrelated schools.
_FILLER_RE = re.compile(
    r"\b("
    r"school|elementary|middle|high|academy|center|complex|"
    r"public|charter|the|and"
    r")\b",
    re.IGNORECASE,
)
# Common school suffixes/abbreviations that should also be neutralized.
# NOTE: don't include "no.\s*\d+" here — that would eat the school number
# before _extract_school_number can pull it out.
_ABBREV_RE = re.compile(r"\b(e\.?s\.?|m\.?s\.?|h\.?s\.?|jr\.?|sr\.?)\b", re.IGNORECASE)
# Anything not alphanumeric or whitespace
_PUNCT_RE = re.compile(r"[^\w\s]")

# Patterns that pull a "school number" out of names like:
#   "PS #10", "PS10", "PS 10", "P.S. 10",
#   "School #10", "School No. 10", "School 10",
#   "Public School #10", "School #6/Middle School"
# The number must not be preceded by another digit (so "2024" isn't caught)
# and not preceded by "Grade"/"Grades" (so "Grade 7" doesn't collide with
# a school number). We deliberately do NOT try to pull numbers out of
# arbitrary strings like "Building 42" — only school-number contexts.
_SCHOOL_NUM_PATTERNS = [
    # "PS 10", "P.S. #10", "P S 10"
    re.compile(r"\bp\.?\s*s\.?\s*#?\s*(\d{1,3})\b", re.IGNORECASE),
    # "School #10", "School No. 10", "School 10"
    re.compile(r"\bschool\s*(?:no\.?\s*)?#?\s*(\d{1,3})\b", re.IGNORECASE),
    # Bare leading "#10 …" or "…/School 10"
    re.compile(r"#\s*(\d{1,3})\b"),
]


def _extract_school_number(name: str | None) -> int | None:
    """
    Pull the numeric identifier out of a numbered school name.

    Returns None if the name doesn't look numbered.  Examples:
      'PS#10'                    -> 10
      'Ps 28'                    -> 28
      'Public School #4'         -> 4
      'School #6/Middle School'  -> 6
      'Charles J. Riley/School 9'-> 9
      'John F. Kennedy High'     -> None
      'Building 42'              -> None  (not a school-number context)
    """
    if not name:
        return None
    for pat in _SCHOOL_NUM_PATTERNS:
        m = pat.search(name)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _normalize(name: str | None) -> str:
    """Lowercase, strip filler/punctuation, collapse spaces."""
    if not name:
        return ""
    s = name.lower()
    s = _ABBREV_RE.sub(" ", s)
    s = _FILLER_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# District normalization — different from school-name normalization because
# district strings have their own noise patterns (BOE, MUA, suffixes).
_DISTRICT_ABBREVS = {
    "boe": "board of education",
    "wpboe": "woodland park board of education",
    "mua": "municipal utilities authority",
    "twp": "township",
    "boro": "borough",
    "bd": "board",
    "ed": "education",
}

_DISTRICT_NOISE_RE = re.compile(
    r"\b("
    r"board\s+of\s+education"
    r"|municipal\s+utilities?\s+authority"
    r"|public\s+school\s+district"
    r"|public\s+schools?"
    r"|school\s+district"
    r"|school\s+system"
    r"|schools?"
    r"|district"
    r"|regional"
    r")\b",
    re.IGNORECASE,
)


def _normalize_district(name: str | None) -> str:
    """
    Normalize a district name to its core town/township tokens.

    'Wayne Township Board Of Education' → 'wayne township'
    'Ringwood Boe' → 'ringwood'
    'Paterson Public Schools' → 'paterson'
    'Wpboe' → 'woodland park'
    """
    if not name:
        return ""
    s = name.lower().strip()
    # Expand abbreviations first (before stripping noise)
    for abbr, expansion in _DISTRICT_ABBREVS.items():
        s = re.sub(rf"\b{re.escape(abbr)}\b", expansion, s)
    s = _DISTRICT_NOISE_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Strip trailing numbers (e.g. "Ringwood Boe #9041" → "ringwood")
    s = re.sub(r"\s*#?\d+$", "", s).strip()
    return s


# -----------------------------------------------------------------------------
# Matcher
# -----------------------------------------------------------------------------

# Thresholds — empirically picked. The district threshold is intentionally
# more permissive because PDF district names vary a lot ("Trenton",
# "Trenton Public Schools", "Trenton BOE", "City of Trenton School District").
# The district block uses token_set_ratio on filler-stripped names so
# "Paterson Public School District" reduces to "paterson district". At 85
# this keeps "Paterson Public" and "City of Paterson" together but drops
# generic look-alikes like "Verona Public School District" (81) — the town
# name has to actually match.
DISTRICT_BLOCK_THRESHOLD = 85
# 82 rather than 85: catches near-misses like "Dale Ave" vs "Dale Avenue"
# (score 84.2) without opening the door to unrelated matches.
NAME_MATCH_THRESHOLD = 82
# When the school number matched, we already have identity — accept a
# lower text-similarity score, since e.g. "ps 10" vs "school 10" scores
# in the low 60s even though it's the correct match.
NAME_MATCH_THRESHOLD_NUMBERED = 40


class SchoolMatcher:
    """
    Wraps the NCES lookup CSV and provides fuzzy match by (school, district).

    Construct once at the start of an export run and reuse for every row.
    """

    def __init__(self, lookup_csv: Path) -> None:
        # Holds every NCES row, parsed once.
        self._records: list[dict] = []
        # Pre-computed normalized strings, parallel to _records.
        self._norm_names: list[str] = []
        self._norm_districts: list[str] = []
        # Numeric identity for numbered schools, parallel to _records.
        # None for schools without a number in the canonical name.
        self._school_numbers: list[int | None] = []
        # Result cache keyed by (input_name, input_district).
        self._cache: dict[tuple[str, str], SchoolMatch | None] = {}

        with open(lookup_csv, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self._records.append(row)
                self._norm_names.append(_normalize(row["school_name"]))
                self._norm_districts.append(_normalize_district(row["district_name"]))
                self._school_numbers.append(_extract_school_number(row["school_name"]))

        if not self._records:
            raise RuntimeError(f"Lookup file {lookup_csv} is empty")

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def match(self, school_name: str | None, district: str | None) -> SchoolMatch | None:
        """
        Find the best NCES record for (school_name, district).

        Returns None if no candidate clears the threshold.
        """
        if not school_name:
            return None

        cache_key = (school_name or "", district or "")
        if cache_key in self._cache:
            return self._cache[cache_key]

        norm_name = _normalize(school_name)
        norm_district = _normalize_district(district)
        input_number = _extract_school_number(school_name)

        # District is required for identity. Without it, a numbered school
        # like "PS4" would match Paterson OR Belleville OR any district's PS4,
        # and a name-only match like "Rosa Parks" could hit the wrong town.
        # Better to return None (row keeps blank address) than to fabricate
        # a match against the wrong district.
        if not norm_district:
            self._cache[cache_key] = None
            return None

        # Step 1: build the candidate index pool by blocking to the
        # input district. We use token_set_ratio (not WRatio) because the
        # normalized district names are short and share the token
        # "district" — WRatio's partial-match rewards inflate unrelated
        # districts to 85+, while token_set_ratio requires the town-name
        # tokens to actually overlap.
        candidate_indices = [
            i for i, d in enumerate(self._norm_districts)
            if fuzz.token_set_ratio(norm_district, d) >= DISTRICT_BLOCK_THRESHOLD
        ]

        # Step 1b: numeric-identity filter. Numbered schools are their own
        # namespace — "PS #10" must match "School 10", not any bare-named
        # school that happens to fuzzy-match.
        #   - If the input has a number: prefer candidates with the same
        #     number. If none exist in the district (e.g. a school was
        #     renamed and NCES dropped the number), fall through to
        #     unnumbered candidates so a name-only match can still succeed
        #     (e.g. "Ps#30 Mlk" -> "Dr. Martin Luther King Jr. Educational
        #     Complex" via the "MLK" token). We do NOT fall through to
        #     candidates with a *different* number, since that would be an
        #     identity error.
        #   - If the input has no number: exclude candidates that do have
        #     one, so "John F. Kennedy" can't drift into "School 5".
        numeric_identity_matched = False
        if input_number is not None:
            numbered = [i for i in candidate_indices if self._school_numbers[i] == input_number]
            if numbered:
                candidate_indices = numbered
                numeric_identity_matched = True
            else:
                candidate_indices = [
                    i for i in candidate_indices if self._school_numbers[i] is None
                ]
        else:
            candidate_indices = [
                i for i in candidate_indices if self._school_numbers[i] is None
            ]

        if not candidate_indices:
            self._cache[cache_key] = None
            return None

        # Step 2: fuzzy match school name within the blocked pool.
        # process.extractOne picks the single best candidate.
        candidate_names = [(self._norm_names[i], i) for i in candidate_indices]
        best = process.extractOne(
            norm_name,
            [n for n, _ in candidate_names],
            scorer=fuzz.WRatio,
        )
        if best is None:
            self._cache[cache_key] = None
            return None

        _matched_name, score, idx_in_pool = best
        # When the numeric filter established identity, accept lower text
        # similarity ("ps 10" vs "school 10" scores in the 60s but is the
        # correct match). If it did NOT establish identity — either the
        # input has no number, or the input's number had no NCES match in
        # the district — require the regular strict threshold so a name
        # rewrite has to actually be similar.
        threshold = NAME_MATCH_THRESHOLD_NUMBERED if numeric_identity_matched else NAME_MATCH_THRESHOLD
        if score < threshold:
            self._cache[cache_key] = None
            return None

        record_idx = candidate_names[idx_in_pool][1]
        record = self._records[record_idx]

        # Step 3: assemble the SchoolMatch
        try:
            lat = float(record["latitude"]) if record["latitude"] else None
            lon = float(record["longitude"]) if record["longitude"] else None
        except ValueError:
            lat, lon = None, None

        result = SchoolMatch(
            nces_id=record["nces_id"],
            school_name=record["school_name"],
            district_name=record["district_name"],
            county=record["county"],
            address=record["address"],
            latitude=lat,
            longitude=lon,
            confidence=float(score),
        )
        self._cache[cache_key] = result
        return result
