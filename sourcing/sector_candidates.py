"""SRC-17: robotics, space/aerospace and materials companies from the Crunchbase harvest.

Two steps, both standalone:

  candidates   — writes the resolver input for every candidate in the three
                 sectors that has no board yet: US, operating, in cb_rank
                 order, category already set to its sector, plus a seed-URL
                 file from the harvest's website column. Prints per-sector
                 counts (candidates, already resolved, to resolve).

  recategorize — rewrites the category column of startups_ats_resolved.csv,
                 startups_unresolved.csv and broad_tier_pending.csv so every
                 row in the three sectors carries its sector. Rows are matched
                 to the harvest by crunchbase_slug, then by company name; a row
                 with no harvest match falls back to its own industries column
                 (startups_unresolved.csv has one). Nothing else in a row changes.

Harvest rows, boards and seed URLs are matched on crunchbase_slug; a name is
used only where no slug is known and the name is unique (SRC-19: names repeat).

The sector rules live in sourcing/categories.py.

Usage:
    python3 sourcing/sector_candidates.py candidates --out-dir DIR
    python3 sourcing/sector_candidates.py recategorize
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

SOURCING = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCING))

from categories import SECTOR_ORDER, recategorize, sector_of  # noqa: E402
from harvest import company_lookup, read_harvest, row_key  # noqa: E402

# Choosing sector candidates reads the harvest's industries, description,
# operating_status and hq. Only the full companies_master.csv has them; with
# just the slim companies_min.csv.gz (OPS-11, the runner's copy) read_harvest
# stops and says so. The seed URL (website) is in both.
CANDIDATE_COLUMNS = ("company", "crunchbase_slug", "website", "cb_rank", "industries",
                     "description", "operating_status", "hq")
RECATEGORIZE_COLUMNS = ("company", "crunchbase_slug", "cb_rank", "industries", "description")
RESOLVED = SOURCING / "startups_ats_resolved.csv"
UNRESOLVED = SOURCING / "startups_unresolved.csv"
PENDING = SOURCING / "broad_tier_pending.csv"

INPUT_COLUMNS = ["company", "crunchbase_slug", "category", "hq", "market", "industries", "cb_rank"]

US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
}

csv.field_size_limit(10 ** 9)


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def rank_of(row: dict[str, str]) -> int:
    try:
        return int(row.get("cb_rank") or 0) or 10 ** 9
    except ValueError:
        return 10 ** 9


def is_us(row: dict[str, str]) -> bool:
    """HQ is "City, State" (first two Crunchbase locations); any US state part counts."""
    return any(part.strip() in US_STATES for part in (row.get("hq") or "").split(","))


def is_operating(row: dict[str, str]) -> bool:
    return (row.get("operating_status") or "").strip().lower() == "active"


def cmd_candidates(args: argparse.Namespace) -> None:
    master = read_harvest(CANDIDATE_COLUMNS)
    resolved_rows = read(RESOLVED)
    boards = {row_key(r) for r in resolved_rows if (r.get("ats") or "").strip()}
    tried = {row_key(r) for r in resolved_rows}

    counts = collections.Counter()
    todo: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in sorted(master, key=rank_of):
        cat = sector_of(row["industries"], row["description"], row["company"])
        if not cat:
            continue
        counts[cat, "matched"] += 1
        if not is_operating(row):
            counts[cat, "closed"] += 1
            continue
        if not is_us(row):
            counts[cat, "non_us"] += 1
            continue
        counts[cat, "candidates"] += 1
        key = row_key(row)
        if key in boards:
            counts[cat, "resolved"] += 1
            continue
        if key in seen:
            counts[cat, "duplicate"] += 1
            continue
        seen.add(key)
        counts[cat, "to_resolve"] += 1
        counts[cat, "tried_before" if key in tried else "never_tried"] += 1
        todo.append({**{c: row.get(c, "") for c in INPUT_COLUMNS}, "category": cat,
                     "_website": (row.get("website") or "").strip()})

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / "sector_input.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(todo)
    with (out / "sector_seed_urls.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "crunchbase_slug", "url"])
        w.writerows([r["company"], r["crunchbase_slug"], r["_website"]] for r in todo if r["_website"])

    print(f"{'sector':16s} {'matched':>8s} {'closed':>7s} {'non-US':>7s} {'candid.':>8s} "
          f"{'resolved':>9s} {'to resolve':>11s} {'(tried':>7s} {'never)':>7s}")
    for cat in SECTOR_ORDER:
        print(f"{cat:16s} {counts[cat, 'matched']:8d} {counts[cat, 'closed']:7d} "
              f"{counts[cat, 'non_us']:7d} {counts[cat, 'candidates']:8d} "
              f"{counts[cat, 'resolved']:9d} {counts[cat, 'to_resolve']:11d} "
              f"{counts[cat, 'tried_before']:7d} {counts[cat, 'never_tried']:7d}")
    print(f"\nWrote {out / 'sector_input.csv'} ({len(todo)} companies, cb_rank order)")
    print(f"Wrote {out / 'sector_seed_urls.csv'}")


def cmd_recategorize(args: argparse.Namespace) -> None:
    find = company_lookup(RECATEGORIZE_COLUMNS)

    for path in (RESOLVED, UNRESOLVED, PENDING):
        if not path.exists():
            continue
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            columns = reader.fieldnames or []
            rows = list(reader)
        changed = collections.Counter()
        for row in rows:
            src = find(row.get("crunchbase_slug", ""), row.get("company", ""))
            industries = src["industries"] if src else row.get("industries", "")
            description = src["description"] if src else ""
            new = recategorize(row.get("category", ""), industries, description, row.get("company", ""))
            if new != row.get("category"):
                changed[(row.get("category") or "(none)", new)] += 1
                row["category"] = new
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=columns)
            w.writeheader()
            w.writerows(rows)
        print(f"{path.name}: {sum(changed.values())} rows recategorized")
        for (old, new), n in sorted(changed.items(), key=lambda kv: -kv[1]):
            print(f"  {old:18s} -> {new:16s} {n:5d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--out-dir", type=Path, required=True)
    c.set_defaults(func=cmd_candidates)
    r = sub.add_parser("recategorize")
    r.set_defaults(func=cmd_recategorize)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
