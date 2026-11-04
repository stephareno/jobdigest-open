# tools/build_starter_companies.py — the public segment of the starter list (PUB-15)
#
#   GMAIL_ADDRESS=you@example.com python tools/build_public_companies.py --out /tmp/public.csv
#   python tools/build_starter_companies.py --public /tmp/public.csv
#
# data/company_lists/starter_companies.csv is the one shipped company list:
#   name, website, notes, segment, ticker, sic, sic_description, state, outcome
# segment `public` rows come from a tools/build_public_companies.py build (SEC
# EDGAR + Wikidata) reduced to those columns; segment `startup` rows are the
# companies behind the maintainer's resolved startup-tier boards, written by
# tools/build_starter_boards.py (ticker, sic, sic_description and state empty).
# Public rows come first. Four companies are in both segments, one row each;
# they share one board.
#
# This tool replaces the public rows and keeps the startup rows. A public
# row's `outcome` is the build's own when it has one, else the outcome the
# current file holds for that name (case-insensitive), so a rebuild keeps the
# maintainer's resolve results. data/company_lists/corrections.csv (PUB-13) is
# applied last.
#
# data/company_lists/starter_boards.csv holds the pre-resolved boards for both
# segments (name, ats, url, website): startup rows first (build_starter_boards.py),
# then public rows (build_public_boards.py). A public board that repeats a
# startup row (same name, vendor and token) is written once.
#
# The helpers below are shared by the three build tools.

import argparse
import csv
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_lists  # noqa: E402

COMPANIES = ROOT / "data" / "company_lists" / "starter_companies.csv"
BOARDS = ROOT / "data" / "company_lists" / "starter_boards.csv"
COLUMNS = ["name", "website", "notes", "segment", "ticker", "sic", "sic_description", "state", "outcome"]
BOARD_HEADER = ["name", "ats", "url", "website"]
PROVENANCE = ("# Built {today} by tools/build_starter_companies.py and tools/build_starter_boards.py. "
              "Public segment: US companies listed on NYSE, Nasdaq or CBOE, from SEC EDGAR "
              "(company_tickers_exchange.json and submissions; public domain) and Wikidata (CC0). "
              "Startup segment: the maintainer's resolved startup-tier boards.\n")

csv.field_size_limit(10 ** 9)


def read_companies(path=COMPANIES):
    """[row dict] from the starter list; [] when it does not exist yet."""
    return company_lists.read_list(str(path)) if Path(path).exists() else []


def write_companies(rows, path=COMPANIES, today=None):
    """Public rows first, then startup rows, under a fresh provenance line."""
    today = today or datetime.date.today().isoformat()
    ordered = [r for r in rows if r.get("segment") == "public"] + [r for r in rows if r.get("segment") != "public"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        fh.write(PROVENANCE.format(today=today))
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n", extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(ordered)


def segment_names(rows, segment):
    return {r["name"].casefold() for r in rows if r.get("segment") == segment}


def board_key(b):
    """A board row's identity: name (case-insensitive), vendor and token
    (the URL for a careers page, which has no token)."""
    return b["name"].casefold(), b["ats"], company_lists.board_token(b["ats"], b["url"]) or b["url"]


def read_boards(path=BOARDS):
    if not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_boards(rows, path=BOARDS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=BOARD_HEADER, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def public_rows(built, current):
    """A tools/build_public_companies.py build as starter-list rows, each
    keeping the outcome `current` holds for its name when the build has none."""
    old = {r["name"].casefold(): r.get("outcome", "") for r in current if r.get("segment") == "public"}
    out = []
    for r in built:
        row = {c: r.get(c, "") for c in COLUMNS}
        row["segment"] = "public"
        row["outcome"] = r.get("outcome") or old.get(r["name"].casefold(), "")
        out.append(row)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replace the public segment of starter_companies.csv")
    parser.add_argument("--public", required=True,
                        help="a tools/build_public_companies.py build (its --out file)")
    parser.add_argument("--out", default=str(COMPANIES), help="write here instead of the shipped file")
    args = parser.parse_args(argv)
    current = read_companies(args.out)
    public = public_rows(company_lists.read_list(args.public), current)
    startup = [r for r in current if r.get("segment") == "startup"]
    rows = company_lists.apply_corrections(public + startup)
    write_companies(rows, args.out)
    print(f"wrote {args.out}: {len(public)} public and {len(startup)} startup companies")


if __name__ == "__main__":
    main()
