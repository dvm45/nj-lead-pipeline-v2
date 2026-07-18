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
# Common school suffixes/abbreviations that should also be neutralized
_ABBREV_RE = re.compile(r"\b(e\.?s\.?|m\.?s\.?|h\.?s\.?|jr\.?|sr\.?|no\.?\s*\d+)\b", re.IGNORECASE)
# Anything not alphanumeric or whitespace
_PUNCT_RE = re.compile(r"[^\w\s]")


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


# -----------------------------------------------------------------------------
# Matcher
# -----------------------------------------------------------------------------

# Thresholds — empirically picked. The district threshold is intentionally
# more permissive because PDF district names vary a lot ("Trenton",
# "Trenton Public Schools", "Trenton BOE", "City of Trenton School District").
DISTRICT_BLOCK_THRESHOLD = 75
NAME_MATCH_THRESHOLD = 85


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
        # Result cache keyed by (input_name, input_district).
        self._cache: dict[tuple[str, str], SchoolMatch | None] = {}

        with open(lookup_csv, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self._records.append(row)
                self._norm_names.append(_normalize(row["school_name"]))
                self._norm_districts.append(_normalize(row["district_name"]))

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
        norm_district = _normalize(district)

        # Step 1: build the candidate index pool.
        # If we have a district, block to records in that district.
        # If not, search the full universe — slower but still feasible at 2,500 rows.
        if norm_district:
            candidate_indices = [
                i for i, d in enumerate(self._norm_districts)
                if fuzz.WRatio(norm_district, d) >= DISTRICT_BLOCK_THRESHOLD
            ]
        else:
            candidate_indices = list(range(len(self._records)))

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
        if score < NAME_MATCH_THRESHOLD:
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
