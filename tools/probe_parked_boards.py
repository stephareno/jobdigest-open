#!/usr/bin/env python3
"""Re-probe parked boards and watchlist entries; optionally park ones that fail.

Run monthly by .github/workflows/resolve_ats.yml, after the ATS re-resolve,
whose changes land as a PR — so any parking done here is reviewed before it
takes effect. Nothing is ever deleted:

  * every parked row in sourcing/parked_boards.csv is fetched again with the
    same headers, endpoint and retry the daily run uses — startup boards,
    watchlist Workday tenants (ats=workday) and watchlist career URLs
    (ats=careers_page). Rows that now succeed are unparked; rows that still
    fail keep their reason text and get a fresh last_checked date.
  * with --park-failures, every active startup board and every
    pinned Workday tenant (data/companies.yaml pinned.workday) is probed too, and one that fails is parked
    with its reason.

Standalone: python tools/probe_parked_boards.py [--park-failures]
"""

import argparse
import concurrent.futures as cf
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.watchlist import WORKDAY_TENANTS  # noqa: E402
from sources import startups  # noqa: E402
from sources.ats import error_kind, fetch_one_board, get_with_retry  # noqa: E402
from sources.watchlist import workday_key  # noqa: E402


RESOLVE_REASONS = ("unsupported vendor:", "resolve:")


def _probe(board):
    """(board, None) on success, (board, reason) on failure."""
    try:
        if board["ats"] == "careers_page":
            get_with_retry(board["board_url"])
            return board, None
        result = fetch_one_board(board["company"], board["ats"], board["slug"], board["board_url"],
                                 source="startups")
        if result.error is not None:
            return board, f"{error_kind(result.error)}: {str(result.error)[:160]}"
        return board, None
    except Exception as e:
        return board, f"{error_kind(e)}: {str(e)[:160]}"


def _watchlist_tenants():
    """Every pinned Workday tenant, in the board shape _probe takes."""
    boards = []
    for company, cfg in WORKDAY_TENANTS.items():
        _, token = workday_key(cfg)
        boards.append({"company": company, "ats": "workday", "slug": token,
                       "board_url": f"{cfg['host']}/{cfg['site']}"})
    return boards


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--park-failures", action="store_true",
                        help="also probe active boards and Workday tenants, and park the ones that fail")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    today = date.today().isoformat()
    parked = startups.read_parked()

    candidates = startups.load_boards(include_parked=True) + _watchlist_tenants()
    boards, seen = [], set()
    for b in candidates:
        key = (b["ats"], b["slug"])
        if key in seen:
            continue
        seen.add(key)
        if args.park_failures or key in parked:
            boards.append(b)
    # Parked rows with no active counterpart (watchlist career URLs, retired tenants).
    # PUB-11: rows `main.py resolve` parked are companies with no board to
    # probe (an unsupported vendor, or nothing found); they are left as they are.
    for key, row in parked.items():
        if (row.get("reason") or "").startswith(RESOLVE_REASONS):
            continue
        if key not in seen:
            boards.append({"company": row["company"], "ats": row["ats"],
                           "slug": row["board_token"], "board_url": row["board_url"]})

    print(f"[Probe] probing {len(boards)} boards ({len(parked)} currently parked)")
    with cf.ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(_probe, boards))

    unparked, still, newly = [], 0, 0
    for board, error in results:
        key = (board["ats"], board["slug"])
        if error is None:
            if key in parked:
                unparked.append(parked.pop(key))
            continue
        if key in parked:
            parked[key].update(last_checked=today)
            print(f"[Probe]   still failing {board['company']} ({board['ats']}/{board['slug']}): {error}")
            still += 1
        elif args.park_failures:
            parked[key] = {"ats": board["ats"], "board_token": board["slug"],
                           "company": board["company"], "board_url": board["board_url"],
                           "reason": error, "parked_on": today, "last_checked": today}
            newly += 1

    startups.write_parked(parked.values())
    print(f"[Probe] unparked {len(unparked)}, still parked {still}, newly parked {newly}, "
          f"total parked now {len(parked)}")
    for row in unparked:
        print(f"[Probe]   unparked {row['company']} ({row['ats']}/{row['board_token']})")


if __name__ == "__main__":
    main()
