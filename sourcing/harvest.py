"""OPS-11: one place that reads the Crunchbase harvest.

sourcing/companies_master.csv is the full harvest (~58k rows, every column). It
is gitignored, so it exists only on a machine it was copied to.
sourcing/companies_min.csv.gz is a committed slim copy with five columns
(company, crunchbase_slug, website, category, cb_rank), rebuilt from the full
file by tools/build_companies_min.py. It is what the GitHub Actions runner has.

read_harvest() prefers the full file when it is present and falls back to the
slim one. A caller names the columns it needs; if only the slim copy is here
and it lacks one of them, the run stops with a message saying so rather than
working from blank values.
"""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

SOURCING = Path(__file__).resolve().parent
MASTER = SOURCING / "companies_master.csv"
MIN = SOURCING / "companies_min.csv.gz"
MIN_COLUMNS = ["company", "crunchbase_slug", "website", "category", "cb_rank"]

csv.field_size_limit(10 ** 9)


def harvest_path() -> Path:
    """The harvest file this machine has: the full one if present, else the slim one."""
    if MASTER.exists():
        return MASTER
    if MIN.exists():
        return MIN
    raise SystemExit(f"No harvest found: neither {MASTER} (gitignored, local only) nor {MIN} "
                     f"(committed; rebuild with tools/build_companies_min.py)")


def read_harvest(needs: tuple[str, ...] = ("company", "website")) -> list[dict[str, str]]:
    """Every harvest row. `needs` names the columns the caller relies on."""
    path = harvest_path()
    if path == MIN:
        missing = [c for c in needs if c not in MIN_COLUMNS]
        if missing:
            raise SystemExit(f"{MASTER.name} is absent and {MIN.name} has no "
                             f"{', '.join(missing)} column(s). This step needs the full harvest; "
                             f"copy companies_master.csv into sourcing/ and run it locally.")
        with gzip.open(path, "rt", newline="") as fh:
            rows = list(csv.DictReader(fh))
    else:
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    print(f"[Harvest] {len(rows)} companies from {path.name}")
    return rows


def _rank(row: dict[str, str]) -> int:
    rank = (row.get("cb_rank") or "").strip()
    return int(rank) if rank.isdigit() else 10 ** 9


def row_key(row: dict[str, str]) -> str:
    """A company's identity in the sourcing CSVs: its crunchbase_slug, else its name.

    Company names repeat in the harvest (SRC-19: SRC-18 checked 101 companies
    against a same-named company's website), so nothing that joins a company
    to the harvest, a website or a seed URL may key on the name while a slug
    is known. Name-only keys carry a "name:" prefix so they never equal a slug.
    """
    slug = (row.get("crunchbase_slug") or "").strip().lower()
    return slug or "name:" + (row.get("company") or "").strip().lower()


def company_lookup(needs: tuple[str, ...] = ("company", "website")):
    """A function (crunchbase_slug, company) -> that company's harvest row, or None.

    The slug decides whenever it is in the harvest, even if that row's website
    is blank: a blank website is an answer, not a reason to borrow a namesake's.
    The name is used only when the slug is absent or unknown, and only when
    exactly one harvest company has that name.
    """
    rows = read_harvest(tuple(dict.fromkeys(("company", "crunchbase_slug") + needs)))
    by_slug: dict[str, dict[str, str]] = {}
    by_name: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        slug = (r.get("crunchbase_slug") or "").strip().lower()
        if slug:
            by_slug.setdefault(slug, r)
        name = (r.get("company") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(r)

    def lookup(slug: str, company: str) -> dict[str, str] | None:
        hit = by_slug.get((slug or "").strip().lower())
        if hit is not None:
            return hit
        same_name = by_name.get((company or "").strip().lower(), [])
        return same_name[0] if len(same_name) == 1 else None
    return lookup


def website_lookup():
    """A function (crunchbase_slug, company) -> harvest website ("" when none), via company_lookup()."""
    find = company_lookup(("company", "crunchbase_slug", "website"))

    def lookup(slug: str, company: str) -> str:
        return ((find(slug, company) or {}).get("website") or "").strip()
    return lookup
