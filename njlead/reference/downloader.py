"""
NCES reference data downloader.

NCES publishes two files we need to enrich school records:

  1. CCD Public School Universe Survey ("Directory")
     — has school name, district name, address, county
     — distributed as a CSV inside a ZIP
     — landing page: https://nces.ed.gov/ccd/files.asp
     — find the file named like ccd_sch_029_<YY><YY>_w_1a_<MMDDYY>.zip

  2. EDGE Geocoded School Locations
     — has latitude, longitude, joined to schools by NCESSCH ID
     — distributed as an XLSX
     — landing page: https://nces.ed.gov/programs/edge/Geographic/SchoolLocations
     — find the file named EDGE_GEOCODE_PUBLICSCH_<YYYY>.xlsx

Together they cover every public school in NJ (~2,500 records).

These URLs include date stamps in the filenames, so they change when NCES
publishes a new release. The constants below should be reviewed annually.
If a download 404s, visit the landing page above, copy the new file URL,
and update the constant.
"""

import csv
import urllib.request
import zipfile
from pathlib import Path

# -----------------------------------------------------------------------------
# URLs — update these when NCES publishes a new release
# -----------------------------------------------------------------------------
# Format: ccd_sch_029_<endYY><startYY?>_w_1a_<MMDDYY>.zip
# 2324 = the 2023-24 school year
CCD_DIRECTORY_URL = (
    "https://nces.ed.gov/ccd/Data/zip/ccd_sch_029_2324_w_1a_073124.zip"
)
# Format: EDGE_GEOCODE_PUBLICSCH_<endYYYY>.xlsx
EDGE_GEOCODE_URL = (
    "https://nces.ed.gov/programs/edge/data/EDGE_GEOCODE_PUBLICSCH_2324.xlsx"
)

# A polite User-Agent — some servers reject default urllib (Python-urllib/3.x)
_HTTP_HEADERS = {
    "User-Agent": "njlead-pipeline/1.0 (research data tool)",
}


# -----------------------------------------------------------------------------
# Low-level: streaming download
# -----------------------------------------------------------------------------

def _download(url: str, dest: Path) -> Path:
    """
    Stream a file from `url` to `dest`, creating parent dirs as needed.

    Uses urllib.request from the stdlib — no extra dependency.
    Streams in 64KB chunks so large files don't blow up memory.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(request) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            out.write(chunk)
    return dest


# -----------------------------------------------------------------------------
# CCD School Directory: download, unzip, filter to NJ
# -----------------------------------------------------------------------------

def download_ccd(year: int, dest_dir: Path) -> Path:
    """
    Download the CCD Public School directory ZIP, unzip the CSV inside it,
    filter to NJ rows, and write a slimmed CSV to dest_dir.

    Returns the path to the NJ-only CSV.

    Output columns kept (verbatim from CCD):
      NCESSCH, LEAID, SCH_NAME, LEA_NAME,
      LSTREET1, LSTREET2, LCITY, LSTATE, LZIP,
      NMCNTY
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: download the ZIP to a temp location inside dest_dir
    zip_path = dest_dir / f"ccd_sch_{year}.zip"
    print(f"  downloading CCD directory: {CCD_DIRECTORY_URL}")
    _download(CCD_DIRECTORY_URL, zip_path)

    # Step 2: open the ZIP without extracting everything; find the CSV
    with zipfile.ZipFile(zip_path) as zf:
        # The archive normally has one CSV at the root
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(f"No CSV found inside {zip_path}")
        # Pick the largest CSV (skip any small README/metadata CSVs)
        csv_name = max(csv_names, key=lambda n: zf.getinfo(n).file_size)

        # Step 3: stream-read the CSV, filter to NJ, write a slim version
        out_path = dest_dir / f"ccd_nj_{year}.csv"
        keep_cols = [
            "NCESSCH", "LEAID", "SCH_NAME", "LEA_NAME",
            "LSTREET1", "LSTREET2", "LCITY", "LSTATE", "LZIP", "NMCNTY",
        ]

        with zf.open(csv_name) as zfile, open(out_path, "w", newline="", encoding="utf-8") as out:
            # Decode the CSV stream (CCD is UTF-8 with BOM in some years)
            text_stream = (line.decode("utf-8-sig") for line in zfile)
            reader = csv.DictReader(text_stream)

            # Validate that the columns we want are present
            missing = [c for c in keep_cols if c not in reader.fieldnames]
            if missing:
                raise RuntimeError(
                    f"CCD CSV is missing expected columns: {missing}\n"
                    f"Available columns: {reader.fieldnames}"
                )

            writer = csv.DictWriter(out, fieldnames=keep_cols)
            writer.writeheader()
            kept = 0
            for row in reader:
                if row.get("LSTATE", "").strip().upper() == "NJ":
                    writer.writerow({c: row.get(c, "") for c in keep_cols})
                    kept += 1

        print(f"  CCD -> {out_path} ({kept} NJ schools)")

    # Tidy up: remove the big ZIP — the slimmed CSV is enough
    zip_path.unlink(missing_ok=True)
    return out_path


# -----------------------------------------------------------------------------
# EDGE Geocodes: download XLSX, filter to NJ, write CSV
# -----------------------------------------------------------------------------

def download_edge_geocodes(year: int, dest_dir: Path) -> Path:
    """
    Download the EDGE geocode XLSX, extract NJ rows, and write to a CSV.

    Returns the path to the NJ-only geocode CSV. Columns:
      NCESSCH, LAT, LON
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    xlsx_path = dest_dir / f"edge_geocodes_{year}.xlsx"
    print(f"  downloading EDGE geocodes: {EDGE_GEOCODE_URL}")
    _download(EDGE_GEOCODE_URL, xlsx_path)

    # Lazy import so installing this package doesn't fail if openpyxl is missing
    # (e.g. if a user only ever runs `njlead init` and `njlead ingest`).
    from openpyxl import load_workbook

    print(f"  reading {xlsx_path.name} ...")
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active

    # Read the header row to find column positions
    rows = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(rows)]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            raise RuntimeError(
                f"EDGE XLSX missing column '{name}'. Header: {header}"
            )

    ncessch_idx = col("NCESSCH")
    lat_idx = col("LAT")
    lon_idx = col("LON")
    state_idx = col("STATE") if "STATE" in header else col("LSTATE")

    out_path = dest_dir / f"edge_nj_{year}.csv"
    kept = 0
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["NCESSCH", "LAT", "LON"])
        for row in rows:
            if row is None or len(row) <= max(ncessch_idx, lat_idx, lon_idx, state_idx):
                continue
            state = str(row[state_idx] or "").strip().upper()
            if state != "NJ":
                continue
            ncessch = str(row[ncessch_idx] or "").strip()
            lat = row[lat_idx]
            lon = row[lon_idx]
            if not ncessch or lat is None or lon is None:
                continue
            writer.writerow([ncessch, lat, lon])
            kept += 1

    wb.close()
    xlsx_path.unlink(missing_ok=True)
    print(f"  EDGE -> {out_path} ({kept} NJ geocodes)")
    return out_path


# -----------------------------------------------------------------------------
# Build the joined lookup file
# -----------------------------------------------------------------------------

def build_lookup(ccd_csv: Path, edge_csv: Path, out_path: Path) -> Path:
    """
    Join the slimmed CCD directory and EDGE geocodes on NCESSCH and write
    the unified lookup CSV consumed by the matcher.

    Output columns:
      nces_id, school_name, district_name,
      county, address, latitude, longitude
    """
    # Step 1: load EDGE coords into a dict for fast lookup by NCES ID
    coords: dict[str, tuple[str, str]] = {}
    with open(edge_csv, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            coords[row["NCESSCH"]] = (row["LAT"], row["LON"])

    # Step 2: stream the CCD directory and write the joined output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ccd_csv, "r", newline="", encoding="utf-8") as src, \
         open(out_path, "w", newline="", encoding="utf-8") as out:

        reader = csv.DictReader(src)
        writer = csv.DictWriter(
            out,
            fieldnames=[
                "nces_id", "school_name", "district_name",
                "county", "address", "latitude", "longitude",
            ],
        )
        writer.writeheader()

        joined = 0
        for row in reader:
            nces_id = row["NCESSCH"].strip()
            lat, lon = coords.get(nces_id, ("", ""))

            # Concatenate address pieces, skipping blanks
            address_parts = [
                row.get("LSTREET1", "").strip(),
                row.get("LSTREET2", "").strip(),
                row.get("LCITY", "").strip(),
                row.get("LSTATE", "").strip(),
                row.get("LZIP", "").strip(),
            ]
            address = ", ".join(p for p in address_parts if p)

            writer.writerow({
                "nces_id": nces_id,
                "school_name": row.get("SCH_NAME", "").strip(),
                "district_name": row.get("LEA_NAME", "").strip(),
                "county": row.get("NMCNTY", "").strip(),
                "address": address,
                "latitude": lat,
                "longitude": lon,
            })
            joined += 1

    print(f"  lookup -> {out_path} ({joined} schools)")
    return out_path


# -----------------------------------------------------------------------------
# Entry point used by the CLI
# -----------------------------------------------------------------------------

def refresh_all(year: int, dest_dir: Path) -> Path:
    """
    Download both NCES files, build the joined lookup, return the lookup path.

    The CLI calls this from `njlead refresh-reference`.
    """
    ccd = download_ccd(year, dest_dir)
    edge = download_edge_geocodes(year, dest_dir)
    lookup = dest_dir / f"nj_schools_{year}.csv"
    return build_lookup(ccd, edge, lookup)
