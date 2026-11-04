"""Merge a resolver run's output into the standing resolved/unresolved CSVs.

resolve_crunchbase_ats.py writes only the companies in the input list it was
given, so pointing it at a new candidate list and letting it write
startups_ats_resolved.csv directly would drop every company from earlier runs.
This merges instead: a company keeps its resolved board if it has one, and a
newly resolved row wins over a previously unresolved one. Companies are matched
on crunchbase_slug, else name (SRC-19).

Unresolved detail columns (industries, slug_candidates, domains_tried) live only
in the unresolved CSVs, so those are merged from the same pair of files.

Usage:
    python3 sourcing/merge_ats_resolved.py \
        --new-resolved run/active_resolved.csv \
        --new-unresolved run/active_unresolved.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harvest import row_key  # noqa: E402

SOURCING = Path(__file__).resolve().parent
DEFAULT_RESOLVED = SOURCING / "startups_ats_resolved.csv"
DEFAULT_UNRESOLVED = SOURCING / "startups_unresolved.csv"

PASSTHROUGH = ["company", "crunchbase_slug", "category", "hq", "market"]
OUTPUT_COLUMNS = PASSTHROUGH + ["ats", "board_token", "board_url", "method", "careers_url", "has_open_jobs"]
UNRESOLVED_COLUMNS = PASSTHROUGH + ["industries", "reason", "careers_url",
                                    "rejected_board", "slug_candidates", "domains_tried"]

csv.field_size_limit(10 ** 9)


def key_of(row: dict[str, str]) -> str:
    """crunchbase_slug, else name (SRC-19: company names repeat; see harvest.row_key)."""
    return row_key(row)


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as fh:
        return [r for r in csv.DictReader(fh) if (r.get("company") or "").strip()]


def write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def board_key(row: dict[str, str]) -> tuple[str, str] | None:
    ats, token = (row.get("ats") or "").strip(), (row.get("board_token") or "").strip()
    return (ats, token) if ats and token else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--new-resolved", type=Path, required=True)
    ap.add_argument("--new-unresolved", type=Path, required=True)
    ap.add_argument("--resolved", type=Path, default=DEFAULT_RESOLVED)
    ap.add_argument("--unresolved", type=Path, default=DEFAULT_UNRESOLVED)
    args = ap.parse_args()

    old_rows = read(args.resolved)
    new_rows = read(args.new_resolved)
    new_by_key = {key_of(r): r for r in new_rows}

    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    newly_resolved = 0
    rejected_collisions: list[tuple[dict[str, str], str]] = []

    # SRC-16: a board already claimed by a different company must not be
    # claimed again by a careers_page hit — that is exactly how Prepared
    # ended up carrying Axon's board. Standing claims (from any earlier run)
    # win; a same-run collision keeps whichever row is seen first.
    boards_claimed_by: dict[tuple[str, str], str] = {}
    for row in old_rows:
        bk = board_key(row)
        if bk:
            boards_claimed_by[bk] = key_of(row)

    for row in old_rows:
        k = key_of(row)
        if k not in merged:
            order.append(k)
        merged[k] = row

    for row in new_rows:
        k = key_of(row)
        prior = merged.get(k)
        bk = board_key(row)
        if bk and row.get("method") == "careers_page":
            owner = boards_claimed_by.get(bk)
            if owner is not None and owner != k:
                rejected_collisions.append((row, owner))
                if prior is None:
                    # Still needs a row somewhere, or the company silently
                    # vanishes from both CSVs instead of landing in unresolved.
                    order.append(k)
                    merged[k] = {**{c: row.get(c, "") for c in PASSTHROUGH},
                                 "ats": "", "board_token": "", "board_url": "",
                                 "method": "", "careers_url": "", "has_open_jobs": ""}
                continue
        if bk:
            boards_claimed_by.setdefault(bk, k)
        if prior is None:
            order.append(k)
            merged[k] = row
            continue
        # A row only ever gains a board; never let a miss overwrite a hit.
        if (row.get("ats") or "").strip() and not (prior.get("ats") or "").strip():
            merged[k] = row
            newly_resolved += 1
        elif (row.get("ats") or "").strip():
            merged[k] = row

    out_rows = [merged[k] for k in order]

    collision_detail = {key_of(row): owner for row, owner in rejected_collisions}

    detail = {key_of(r): r for r in read(args.unresolved)}
    detail.update({key_of(r): r for r in read(args.new_unresolved)})
    unresolved_rows = []
    for row in out_rows:
        if (row.get("ats") or "").strip():
            continue
        k = key_of(row)
        if k in collision_detail:
            owner = collision_detail[k]
            rejected_row = next(r for r, o in rejected_collisions if key_of(r) == k)
            bk = board_key(rejected_row)
            unresolved_rows.append({
                **{c: row.get(c, "") for c in PASSTHROUGH},
                "industries": "", "reason": "board_claimed_by_other_company",
                "careers_url": rejected_row.get("careers_url", ""),
                "rejected_board": f"{bk[0]}/{bk[1]} — already claimed by {owner}" if bk else "",
                "slug_candidates": "", "domains_tried": "",
            })
            continue
        d = detail.get(k, {})
        unresolved_rows.append({
            **{c: row.get(c, "") for c in PASSTHROUGH},
            "industries": d.get("industries", ""),
            "reason": d.get("reason", ""),
            "careers_url": d.get("careers_url", ""),
            "rejected_board": d.get("rejected_board", ""),
            "slug_candidates": d.get("slug_candidates", ""),
            "domains_tried": d.get("domains_tried", ""),
        })

    write(args.resolved, OUTPUT_COLUMNS, out_rows)
    write(args.unresolved, UNRESOLVED_COLUMNS, unresolved_rows)

    resolved = [r for r in out_rows if (r.get("ats") or "").strip()]
    if rejected_collisions:
        print(f"rejected (SRC-16 guard) : {len(rejected_collisions)} careers_page hit(s) "
              f"landed on a board already claimed by a different company:")
        for row, owner in rejected_collisions:
            bk = board_key(row)
            print(f"  {row.get('company', '')!r} -> {bk[0]}/{bk[1]} (already claimed by {owner!r})")
    print(f"merged companies : {len(out_rows)}")
    print(f"  resolved       : {len(resolved)} ({len(resolved) / len(out_rows) * 100:.1f}%)")
    print(f"  unresolved     : {len(unresolved_rows)}")
    print(f"  new hits on companies previously unresolved: {newly_resolved}")
    print("\nBy ATS platform:")
    for ats, n in collections.Counter(r["ats"] for r in resolved).most_common():
        print(f"  {ats:16s} {n:5d}")
    print(f"\nWrote {args.resolved}\nWrote {args.unresolved}")


if __name__ == "__main__":
    main()
