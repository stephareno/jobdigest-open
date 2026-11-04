#!/usr/bin/env python3
"""OPS-11: monthly re-check of the companies SRC-18 dropped from Bespoke.

sourcing/bespoke_dropped.csv lists companies whose careers_url sat on someone
else's domain and whose own website confirmed no careers page. This asks each
company's own website again through a seed-only resolver pass: read the site
and what it links to, never a guessed domain and never the old careers_url.

Per company, one of:
  * board     -- the site now links to a supported ATS board. The company's row
                 in startups_ats_resolved.csv gets the board and its
                 startups_unresolved.csv row goes, the same move
                 merge_ats_resolved.py makes. A board another company already
                 claims is refused (the SRC-16 guard) and the company stays dropped.
  * bespoke   -- the site now confirms a careers page Bespoke can read, on
                 the company's own domain, the domain its website redirects to
                 (a rebrand) or a shared ATS host. Its startups_unresolved.csv
                 row takes the new reason and careers_url, so it re-enters the
                 Bespoke list.
  * held      -- the page found is on some other domain the site links to
                 (a parent, a sister company, or a domain that has changed
                 hands). That is how these companies got a wrong page in the
                 first place, so it stays dropped and the run logs it for review.
  * still dropped -- nothing changes but last_checked.

Every re-checked row in bespoke_dropped.csv gets last_checked = today. A
company that came back gets reentered_on = today and is not re-checked again;
the row itself stays as the record (recorded, never deleted).

Rows are edited in place, so a month with no change leaves both sourcing CSVs
byte-identical. Nothing is written to main: resolve_ats.yml commits the files
to a branch and opens a PR for review.

The website comes from the harvest (sourcing/harvest.py: companies_master.csv
when present, else the committed companies_min.csv.gz), matched on
crunchbase_slug because company names repeat, falling back to the website
recorded in bespoke_dropped.csv. Pages are fetched fresh: the page cache
defaults to a new temporary directory, because a cached page would only repeat
last month's answer.

Usage:
    python3 sourcing/recheck_dropped.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

import httpx

SOURCING = Path(__file__).resolve().parent
REPO = SOURCING.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SOURCING))

import config  # noqa: E402
from harvest import row_key, website_lookup  # noqa: E402
from merge_ats_resolved import OUTPUT_COLUMNS, UNRESOLVED_COLUMNS, board_key  # noqa: E402
from offdomain_careers import ATS_HOSTS, host_of, on_any, registrable  # noqa: E402
from sources.bespoke import _on_skip_host  # noqa: E402
from tools.resolve_ats import (  # noqa: E402
    ATS_PROBES,
    REQUEST_TIMEOUT,
    USER_AGENT,
    WEBSITE_DELAY_SECONDS,
    HostThrottle,
    PageCache,
    RobotsCache,
    resolve_via_careers,
)

DROPPED = SOURCING / "bespoke_dropped.csv"
RESOLVED = SOURCING / "startups_ats_resolved.csv"
UNRESOLVED = SOURCING / "startups_unresolved.csv"

csv.field_size_limit(10 ** 9)


def read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def bespoke_ready(reason: str, url: str) -> bool:
    """The same test sources/bespoke.py applies when it builds its list."""
    return (url.startswith("http") and reason.startswith(config.BESPOKE_REASON_PREFIXES)
            and not _on_skip_host(url))


def on_own_site(url: str, website: str, landing_host: str) -> bool:
    """The careers page is the company's: its own domain, where its website lands, or an ATS host."""
    own = {registrable(website)} | ({registrable("//" + landing_host)} if landing_host else set())
    return registrable(url) in own or on_any(host_of(url), ATS_HOSTS)


def due_rows(dropped: list[dict[str, str]], unresolved: dict[str, dict[str, str]],
             resolved: dict[str, dict[str, str]]) -> tuple[list[dict[str, str]], collections.Counter]:
    """Rows to re-check this run, oldest last_checked first, and why the rest are skipped."""
    skipped: collections.Counter[str] = collections.Counter()
    due = []
    for row in dropped:
        k = row_key(row)
        if (row.get("reentered_on") or "").strip():
            skipped["came back on an earlier run"] += 1
        elif (resolved.get(k, {}).get("ats") or "").strip():
            skipped["already has a board"] += 1
        elif k not in unresolved:
            skipped["no startups_unresolved.csv row"] += 1
        elif bespoke_ready(unresolved[k].get("reason", ""), unresolved[k].get("careers_url", "")):
            skipped["already back on the Bespoke list"] += 1
        else:
            due.append(row)
    due.sort(key=lambda r: (r.get("last_checked") or "", r["company"].strip().lower()))
    return due, skipped


async def check_all(rows: list[dict[str, str]], sites: dict[str, str], args: argparse.Namespace
                    ) -> list[tuple[dict[str, str], dict[str, Any] | None, dict[str, Any], str]]:
    """(dropped row, board hit or None, report, landing host) for each company, seed-only.

    The landing host is where the website finally answered after redirects,
    read back from the page cache this run just filled."""
    api_throttle = HostThrottle()
    web_throttle = HostThrottle(base_delay=WEBSITE_DELAY_SECONDS)
    semaphore = asyncio.Semaphore(args.max_in_flight)
    cache = PageCache(args.cache_dir)
    robots = RobotsCache()
    limits = httpx.Limits(max_connections=args.max_in_flight + 20,
                          max_keepalive_connections=args.max_in_flight)
    done = 0

    async def one(row: dict[str, str]):
        nonlocal done
        report: dict[str, Any] = {}
        seed = sites[row_key(row)]
        hit = await resolve_via_careers(
            client, row["company"], web_throttle, semaphore, cache, robots,
            seed_url=seed, api_throttle=api_throttle, report=report, seed_only=True)
        landing = (cache.get(seed) or {}).get("final_url") or ""
        done += 1
        if done % 100 == 0 or done == len(rows):
            print(f"[Recheck] progress {done}/{len(rows)}")
        return row, hit, report, host_of(landing) if landing else ""

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True, limits=limits,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
        return await asyncio.gather(*(one(r) for r in rows))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="count what would be re-checked; no network, writes nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="re-check at most N companies (oldest last_checked first)")
    ap.add_argument("--max-in-flight", type=int, default=len(ATS_PROBES) * 4)
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="page cache; default a fresh temporary directory, so every page is fetched now")
    args = ap.parse_args()

    today = date.today().isoformat()
    drop_cols, dropped = read(DROPPED)
    res_cols, resolved_rows = read(RESOLVED)
    unres_cols, unresolved_rows = read(UNRESOLVED)
    if res_cols != OUTPUT_COLUMNS or unres_cols != UNRESOLVED_COLUMNS:
        sys.exit("startups_ats_resolved.csv / startups_unresolved.csv columns differ from "
                 "merge_ats_resolved.py's; refusing to rewrite them")
    resolved = {row_key(r): r for r in resolved_rows}
    unresolved = {row_key(r): r for r in unresolved_rows}

    due, skipped = due_rows(dropped, unresolved, resolved)
    if args.limit is not None:
        due = due[:args.limit]
    harvest_site = website_lookup()
    sites: dict[str, str] = {}
    site_from = collections.Counter()
    for row in due:
        k = row_key(row)
        from_harvest = harvest_site(row.get("crunchbase_slug", ""), row["company"])
        sites[k] = from_harvest or (row.get("website") or "").strip()
        site_from["harvest" if from_harvest else "bespoke_dropped.csv" if sites[k] else "none"] += 1
    no_site = [r for r in due if not sites[row_key(r)]]
    due = [r for r in due if sites[row_key(r)]]

    print(f"[Recheck] {len(dropped)} companies in {DROPPED.name}; re-checking {len(due)} this run")
    for label, n in skipped.most_common():
        print(f"[Recheck]   skipped, {label}: {n}")
    print(f"[Recheck]   website from: {dict(site_from)}")
    if no_site:
        print(f"[Recheck]   skipped, no website anywhere: {len(no_site)}")
    if args.dry_run or not due:
        print("[Recheck] dry run, nothing fetched or written" if args.dry_run else "[Recheck] nothing to do")
        return

    tmp = None
    if args.cache_dir is None:
        tmp = tempfile.TemporaryDirectory(prefix="recheck_cache_")
        args.cache_dir = Path(tmp.name)
    started = time.monotonic()
    results = asyncio.run(check_all(due, sites, args))
    elapsed = time.monotonic() - started
    if tmp:
        tmp.cleanup()

    claimed = {board_key(r): row_key(r) for r in resolved_rows if board_key(r)}
    outcome: collections.Counter[str] = collections.Counter()
    still_reasons: collections.Counter[str] = collections.Counter()
    leave_unresolved: set[str] = set()
    for row, hit, report, landing_host in results:
        k = row_key(row)
        row["last_checked"] = today
        if hit:
            bk = (hit["ats"], hit["slug"])
            owner = claimed.get(bk)
            if owner is not None and owner != k:
                outcome["refused"] += 1
                print(f"[Recheck]   refused {row['company']} -> {bk[0]}/{bk[1]} (claimed by {owner})")
                continue
            claimed[bk] = k
            resolved[k].update(ats=hit["ats"], board_token=hit["slug"], board_url=hit["board_url"],
                               method="careers_page", careers_url=hit.get("careers_url", ""),
                               has_open_jobs="Y" if hit.get("has_open_jobs") else "N")
            leave_unresolved.add(k)
            row["reentered_on"] = today
            outcome["board"] += 1
            print(f"[Recheck]   board   {row['company']} -> {hit['ats']}/{hit['slug']}")
            continue
        reason, url = report.get("reason", ""), report.get("careers_url", "")
        if bespoke_ready(reason, url) and not on_own_site(url, sites[k], landing_host):
            outcome["held"] += 1
            print(f"[Recheck]   held    {row['company']} -> {url} (site {sites[k]}, "
                  f"landed on {landing_host or '?'}): another domain, left dropped for review")
            continue
        if bespoke_ready(reason, url):
            unresolved[k].update(reason=reason, careers_url=url, rejected_board=report.get("rejected", ""))
            row["reentered_on"] = today
            outcome["bespoke"] += 1
            print(f"[Recheck]   bespoke {row['company']} -> {url} ({reason})")
            continue
        outcome["still dropped"] += 1
        still_reasons[reason.split(":")[0] or "(none)"] += 1

    if "reentered_on" not in drop_cols:
        drop_cols.append("reentered_on")
    write(DROPPED, drop_cols, dropped)
    if outcome["board"] or outcome["bespoke"]:
        write(RESOLVED, res_cols, resolved_rows)
        write(UNRESOLVED, unres_cols,
              [r for r in unresolved_rows if row_key(r) not in leave_unresolved])

    print(f"[Recheck] re-checked {len(results)} in {elapsed:.0f}s: board {outcome['board']}, "
          f"back on Bespoke {outcome['bespoke']}, held (page on another domain) {outcome['held']}, "
          f"still dropped {outcome['still dropped']}, "
          f"board refused as another company's (SRC-16) {outcome['refused']}")
    print(f"[Recheck]   still dropped, by reason: {dict(still_reasons.most_common())}")


if __name__ == "__main__":
    main()
