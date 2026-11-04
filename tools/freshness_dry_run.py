"""DEL-05 dry run: how many fetched roles carry a posted date, and how many are fresh.

Runs the fetchers read-only and prints, per source (and per ATS vendor), how
many title-gated roles got a posted date and how many would be flagged fresh.
Optionally renders a digest HTML from those roles so the age text and the flag
can be checked by eye.

Writes nothing the pipeline reads: no seen_jobs.json, no queue, no shard or
bespoke state, no Sheet, no email, no Claude calls. Scores in the HTML are
placeholders (every role is shown at 7), so the order it shows is the
newest-first tiebreak alone. Bespoke is never run (its extraction is paid and
stateful).

    python tools/freshness_dry_run.py
    python tools/freshness_dry_run.py --sources ATS,Watchlist --startup-boards 300 --html /tmp/digest.html

JSearch costs one RapidAPI call per keyword; LinkedIn is off unless named,
since it has no posted date by design and fetches every alert's job page.
"""

import argparse
import os
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import TITLE_GATED_SOURCES, source_key, title_gate  # noqa: E402
from sheet_dates import freshness_counts  # noqa: E402

DEFAULT_SOURCES = ["adzuna", "jsearch", "builtin", "ats", "watchlist", "startups"]


def _fetch_startup_sample(n_boards, seed):
    from sources.ats import fetch_one_board
    from sources.startups import load_boards

    boards = load_boards()
    random.Random(seed).shuffle(boards)
    jobs, failed = [], 0
    for board in boards[:n_boards]:
        try:
            batch = fetch_one_board(board["company"], board["ats"], board["slug"],
                                    board["board_url"], source="startups")
            if batch.error is None:
                for job in batch:
                    job["source"] = "startups"
                    job["tier"] = "startup"
                    jobs.append(job)
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.2)
    print(f"[DryRun] Startup: sampled {min(n_boards, len(boards))} of {len(boards)} boards ({failed} failed)")
    return jobs


def _fetcher(name, args):
    if name == "adzuna":
        from sources.adzuna import fetch_all
    elif name == "jsearch":
        from sources.jsearch import fetch_all
    elif name == "linkedin_email":
        from sources.linkedin_email import fetch_all
    elif name == "builtin":
        from sources.builtin import fetch_all
    elif name == "ats":
        from sources.ats import fetch_all
    elif name == "watchlist":
        from sources.watchlist import fetch_all
    elif name == "startups":
        return lambda: _fetch_startup_sample(args.startup_boards, args.seed)
    else:
        raise SystemExit(f"unknown source {name!r}; choose from {DEFAULT_SOURCES + ['linkedin_email']}")
    return fetch_all


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", default=",".join(DEFAULT_SOURCES))
    parser.add_argument("--startup-boards", type=int, default=200,
                        help="startup-tier boards to sample (the full tier is ~6,000)")
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--html", help="write a digest HTML of the fetched roles to this path")
    args = parser.parse_args()

    # Source keys; a display name ("Startup", "Built In") is accepted too.
    names = [source_key(n.strip()) for n in args.sources.split(",") if n.strip()]
    if "bespoke" in names:
        raise SystemExit("Bespoke is not dry-runnable: its extraction is paid and saves state")

    all_jobs = []
    report = []
    for name in names:
        started = time.monotonic()
        try:
            batch = list(_fetcher(name, args)() or [])
        except Exception as e:
            print(f"[DryRun] {name} FAILED: {e}")
            report.append((name, 0, 0, 0, "FAILED"))
            continue
        if name in TITLE_GATED_SOURCES:
            batch = [j for j in batch if title_gate((j.get("title") or "").strip())]
        dated, fresh = freshness_counts(batch)
        report.append((name, len(batch), dated, fresh, f"{time.monotonic() - started:.0f}s"))
        all_jobs.extend(batch)

    print("\n=== DEL-05 dry run: posted dates per source (roles after the title gate) ===")
    print(f"  {'source':<12} {'roles':>6} {'dated':>6} {'fresh':>6}  time")
    for name, n, dated, fresh, note in report:
        print(f"  {name:<12} {n:>6} {dated:>6} {fresh:>6}  {note}")

    by_vendor = defaultdict(list)
    for job in all_jobs:
        if job.get("ats"):
            by_vendor[job["ats"]].append(job)
    if by_vendor:
        print("\n  by ATS vendor (ATS, Startup and Workday Watchlist roles):")
        for vendor, jobs in sorted(by_vendor.items(), key=lambda kv: -len(kv[1])):
            dated, fresh = freshness_counts(jobs)
            precision = Counter(j.get("posted_precision") or "none" for j in jobs)
            print(f"  {vendor:<16} {len(jobs):>6} {dated:>6} {fresh:>6}  precision {dict(precision)}")

    if args.html:
        os.environ.setdefault("GMAIL_ADDRESS", "dry-run@example.com")
        os.environ.setdefault("GMAIL_APP_PASSWORD", "dry-run")
        import emailer
        import scorer

        for job in all_jobs:
            job.setdefault("score", 7)
            job.setdefault("rationale", "Dry run: placeholder score, not scored.")
        sections = scorer.filter_jobs(all_jobs)
        html = emailer._build_html(sections["main"], [], [], [], False, False)
        Path(args.html).write_text(html)
        print(f"\n[DryRun] digest HTML with {len(sections['main'])} roles -> {args.html}")


if __name__ == "__main__":
    main()
