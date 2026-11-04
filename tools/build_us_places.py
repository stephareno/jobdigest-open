"""Build data/us_places.tsv.gz — the offline US places file location.py reads (FIL-09).

One-off, re-runnable. Downloads GeoNames' US dump (CC BY 4.0,
https://download.geonames.org/export/dump/) and keeps only what the location
resolver uses: populated places and counties, with state, county name,
lat/long and population. The daily run never touches the network for
location; it reads the file this writes.

Usage:
    python3 tools/build_us_places.py            # download and write
    python3 tools/build_us_places.py --src DIR  # use US.txt + admin2Codes.txt already in DIR
"""

import argparse
import gzip
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "data" / "us_places.tsv.gz"
# PUB-09: the country this file covers; profile validation checks
# location.country against it. A builder for another country writes its own code.
COUNTRY_FILE = REPO_ROOT / "data" / "us_places.tsv.gz.country"
COUNTRY = "US"

GEONAMES = "https://download.geonames.org/export/dump/"

# Populated-place feature codes kept. Dropped: PPLQ (abandoned), PPLH
# (historical), PPLW (destroyed), PPLF (farm village), PPLCH (religious centre).
PLACE_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLX", "PPLL", "PPLS"}


def _read_sources(src_dir):
    if src_dir:
        us = Path(src_dir, "US.txt").read_text(encoding="utf-8")
        admin2 = Path(src_dir, "admin2Codes.txt").read_text(encoding="utf-8")
        return us, admin2
    print("Downloading GeoNames US.zip (~70 MB)...")
    with urllib.request.urlopen(GEONAMES + "US.zip") as r:
        us = zipfile.ZipFile(io.BytesIO(r.read())).read("US.txt").decode("utf-8")
    print("Downloading GeoNames admin2Codes.txt...")
    with urllib.request.urlopen(GEONAMES + "admin2Codes.txt") as r:
        admin2 = r.read().decode("utf-8")
    return us, admin2


def build(src_dir=None):
    us, admin2 = _read_sources(src_dir)

    county_names = {}
    for line in admin2.splitlines():
        code, name, *_ = line.split("\t")
        if code.startswith("US."):
            county_names[code[3:]] = name  # "MD.003" -> "Anne Arundel County"

    rows = []
    places = counties = 0
    for line in us.splitlines():
        f = line.split("\t")
        if len(f) < 15:
            continue
        ascii_name, lat, lon, fclass, fcode, state, county, pop = (
            f[2], f[4], f[5], f[6], f[7], f[10], f[11], f[14])
        if not state:
            continue
        if fclass == "P" and fcode in PLACE_CODES:
            kind = "P"
            places += 1
        elif fclass == "A" and fcode == "ADM2":
            kind = "C"
            counties += 1
        else:
            continue
        county_name = county_names.get(f"{state}.{county}", "")
        rows.append("\t".join([
            kind, ascii_name, state, county_name,
            f"{float(lat):.4f}", f"{float(lon):.4f}", pop or "0",
        ]))

    rows.sort()
    OUT.parent.mkdir(exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        fh.write("kind\tname\tstate\tcounty\tlat\tlon\tpopulation\n")
        fh.write("\n".join(rows) + "\n")
    COUNTRY_FILE.write_text(COUNTRY + "\n")
    size_mb = os.path.getsize(OUT) / 1_000_000
    print(f"Wrote {OUT.relative_to(REPO_ROOT)}: {places} places, {counties} counties, {size_mb:.1f} MB")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", help="directory holding US.txt and admin2Codes.txt")
    args = ap.parse_args()
    try:
        build(args.src)
    except Exception as e:
        sys.exit(f"build_us_places failed: {e}")
