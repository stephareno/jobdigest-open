# tools/build_starter_boards.py — the startup segment of the starter list (PUB-11, PUB-15)
#
#   python tools/build_starter_boards.py
#
# Rewrites the startup rows of data/company_lists/starter_boards.csv and of
# data/company_lists/starter_companies.csv, keeping the public rows
# (tools/build_starter_companies.py, tools/build_public_boards.py).
#
# The maintainer's resolved startup-tier boards as a public company list:
# from sourcing/startups_ats_resolved.csv it keeps ONLY the company name, the
# vendor and the board URL the engine fetches, plus an empty website column
# (the lists format). No harvest column (crunchbase_slug, category, hq,
# market, method, ...) is ever copied. Rows are kept in file order; a board
# claimed twice keeps its first claimant, as sources/startups.load_boards
# does. Every row's URL must give back its board token
# (company_lists.board_token), or the run stops. Each company behind them is
# a starter_companies.csv row: segment startup, outcome board (workday for a
# company whose first board is a Workday tenant), no ticker, SIC or state.

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_lists  # noqa: E402
from tools import build_starter_companies as starter  # noqa: E402

SOURCE = ROOT / "sourcing" / "startups_ats_resolved.csv"
OUT = starter.BOARDS
HEADER = starter.BOARD_HEADER

csv.field_size_limit(10 ** 9)


def rows(source=SOURCE):
    out, seen = [], set()
    with open(source, newline="") as fh:
        for r in csv.DictReader(fh):
            name, ats = (r.get("company") or "").strip(), (r.get("ats") or "").strip()
            token, url = (r.get("board_token") or "").strip(), (r.get("board_url") or "").strip()
            if not (name and ats and token and url) or (ats, token) in seen:
                continue
            if company_lists.board_token(ats, url) != token:
                sys.exit(f"{name}: {ats} URL {url!r} does not give back its token {token!r}; "
                         f"teach company_lists.board_token this shape first")
            seen.add((ats, token))
            out.append({"name": name, "ats": ats, "url": url, "website": ""})
    return out


def merged_boards(built, existing, companies):
    """The startup boards, then the public rows `existing` holds (a public
    company's rows that do not repeat a startup board)."""
    public = starter.segment_names(companies, "public")
    keys = {starter.board_key(b) for b in built}
    return built + [b for b in existing if b["name"].casefold() in public and starter.board_key(b) not in keys]


def startup_companies(built, companies):
    """One startup row per company behind the boards, in board order; a row
    already in the list keeps its website and notes."""
    old = {r["name"].casefold(): r for r in companies if r.get("segment") == "startup"}
    out, seen = [], set()
    for b in built:
        key = b["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        prev = old.get(key, {})
        out.append({"name": b["name"], "website": prev.get("website", ""), "notes": prev.get("notes", ""),
                    "segment": "startup", "outcome": "workday" if b["ats"] == "workday" else "board"})
    return out


def main():
    built = rows()
    companies = starter.read_companies()
    boards = merged_boards(built, starter.read_boards(OUT), companies)
    starter.write_boards(boards, OUT)
    startups = startup_companies(built, companies)
    starter.write_companies([r for r in companies if r.get("segment") == "public"] + startups)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(built)} startup boards, {len(boards) - len(built)} public rows kept")
    print(f"wrote {starter.COMPANIES.relative_to(ROOT)}: {len(startups)} startup companies")


if __name__ == "__main__":
    main()
