# ============================================================
# sources/startups.py — Startup-tier ATS boards (PRD §2.9)
# ============================================================
#
# Reads the boards resolved from the Crunchbase-sourced company pool
# (sourcing/startups_ats_resolved.csv, written by sourcing/resolve_crunchbase_ats.py
# and by `python main.py resolve`), plus (PUB-11) the boards the profile's
# watch-tier company lists carry, and pulls each one through the same
# fetchers the dream tier uses.
#
# Identical treatment to the dream tier apart from the tag: same title
# pre-filter (sources/ats._title_passes), same location filter in main.py,
# same scoring. These roles do NOT set is_dream_company, so they take the
# normal score thresholds rather than the dream tier's score>=3 shortcut —
# the tag is for display and tracking, not for ranking.
#
# Boards already covered by the dream tier are skipped here, so a company on
# both lists keeps its dream priority.
#
# Parked boards (sourcing/parked_boards.csv) are not fetched daily. A board is
# parked only after browser headers, the vendor's JSON endpoint and retry with
# backoff have all failed; it keeps its reason and last-checked date and is
# re-probed by the monthly re-resolve (tools/probe_parked_boards.py). Boards
# are never deleted from the board set.
#
# Runtime (PRD §2.9): ~1,400 boards fetched one at a time took ~26 min on the
# Actions runner and killed the 2026-09-10 run. Boards are now fetched by a
# pool of worker threads under a wall-clock budget. When the budget runs out,
# workers stop picking up new boards, the skipped count is logged, and the
# run moves on to scoring with whatever was fetched — a partial startup tier
# never blocks delivery.

import csv
import hashlib
import queue
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from config import (FETCH_RUNS_PER_DAY, STARTUP_FETCH_BUDGET_SECS, STARTUP_FETCH_WORKERS,
                    STARTUP_RUN_MAX_BOARDS, TWO_DAY_VENDOR_ORDER)
from sources.ats import BOARD_DELAY, error_kind, fetch_one_board, postings_line

REPO_ROOT = Path(__file__).resolve().parent.parent
RESOLVED_CSV = REPO_ROOT / "sourcing" / "startups_ats_resolved.csv"
PARKED_CSV = REPO_ROOT / "sourcing" / "parked_boards.csv"
PARKED_FIELDS = ["ats", "board_token", "company", "board_url", "reason", "parked_on", "last_checked"]
COMPANIES_YAML = REPO_ROOT / "data" / "companies.yaml"

# After the budget expires, wait this long for boards already in flight
# (one request is capped at 15s plus retries; a Workday board can page a few
# times) before abandoning them. Worker threads are daemons, so an abandoned
# request cannot hold the process open.
IN_FLIGHT_GRACE_SECS = 45

csv.field_size_limit(10 ** 9)


def _dream_boards():
    """(ats, slug) already fetched by the dream tier, so we do not re-fetch."""
    try:
        with COMPANIES_YAML.open() as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return set()
    return {(e.get("ats"), e.get("slug")) for e in (data.get("companies") or [])
            if e.get("ats") and e.get("slug")}


def read_parked():
    """{(ats, board_token): row} from sourcing/parked_boards.csv."""
    if not PARKED_CSV.exists():
        return {}
    with PARKED_CSV.open(newline="") as fh:
        return {((r.get("ats") or "").strip(), (r.get("board_token") or "").strip()): r
                for r in csv.DictReader(fh)}


def write_parked(rows):
    with PARKED_CSV.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PARKED_FIELDS, lineterminator="\n")
        writer.writeheader()
        for r in sorted(rows, key=lambda r: (r.get("ats", ""), r.get("board_token", ""))):
            writer.writerow({k: r.get(k, "") for k in PARKED_FIELDS})


def load_boards(include_parked=False):
    """Resolved startup boards, deduplicated, minus the dream tier and (by default) parked boards.

    A board can be claimed by several companies — subsidiaries and acquisitions
    share one tenant (four Georgetown entities point at the same Workday site).
    Fetching it once per claimant would repeat the same postings under whichever
    name came first, so the board is fetched once, under its first claimant.
    """
    rows = []
    if RESOLVED_CSV.exists():
        with RESOLVED_CSV.open(newline="") as fh:
            rows = list(csv.DictReader(fh))
    # PUB-11: boards carried by the profile's watch-tier company lists
    # (name, ats, url), after the resolved CSV; a board both carry is one board.
    have = {((r.get("ats") or "").strip(), (r.get("board_token") or "").strip()) for r in rows}
    rows += [{"company": b["company"], "ats": b["ats"], "board_token": b["slug"], "board_url": b["board_url"]}
             for b in _list_boards() if (b["ats"], b["slug"]) not in have]
    if not rows:
        print(f"[Startup] {RESOLVED_CSV.name} not found and no company list carries a board — "
              f"startup tier inactive")
        return []

    dream = _dream_boards()
    parked = read_parked()
    boards, seen = [], set()
    skipped_dream = skipped_parked = dupes = 0
    for r in rows:
        ats = (r.get("ats") or "").strip()
        slug = (r.get("board_token") or "").strip()
        company = (r.get("company") or "").strip()
        url = (r.get("board_url") or "").strip()
        if not (ats and slug and company and url):
            continue
        key = (ats, slug)
        if key in dream:
            skipped_dream += 1
            continue
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        if key in parked and not include_parked:
            skipped_parked += 1
            continue
        boards.append({"company": company, "ats": ats, "slug": slug, "board_url": url})

    if not include_parked:
        by_vendor = Counter(a for a, _ in parked)
        print(f"[Startup] {len(boards)} boards to fetch "
              f"({skipped_dream} already on the dream tier, {dupes} duplicate board tokens, "
              f"{skipped_parked} parked — see sourcing/parked_boards.csv: {dict(by_vendor)})")
    return boards


_error_kind = error_kind


def _list_boards():
    import company_lists
    import profile_loader
    return company_lists.boards(profile_loader.active()["sources"]["company_lists"])


def board_hash(ats, slug):
    """Stable hash of a board (OPS-04): same on every run, whatever the CSV order.

    Keyed on the board, not the company, so a board claimed by several
    companies is still scheduled once.
    """
    key = f"{(ats or '').strip().lower()}:{(slug or '').strip().lower()}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def shard_of(ats, slug, shards):
    """The board's shard when the tier is split into `shards` equal parts."""
    return board_hash(ats, slug) % max(shards, 1)


def in_slot(board, slot, runs_per_day, two_day):
    """Whether a board is fetched in cycle slot `slot` (0 .. 2*runs_per_day - 1).

    Daily vendor: once a day, in the two slots matching hash % runs_per_day.
    Two-day vendor: once every two days, in the one slot matching
    hash % (2*runs_per_day) -- always one of that board's daily slots.
    """
    h = board_hash(board["ats"], board["slug"])
    if board["ats"] in two_day:
        return h % (2 * runs_per_day) == slot
    return h % runs_per_day == slot % runs_per_day


def plan_cadence(boards, runs_per_day=None, max_boards=None, order=None):
    """Vendors moved to the two-day cycle, and the largest run's board count.

    Every vendor stays daily while the largest run fits max_boards. Otherwise
    vendors move to two-day one at a time in `order` until it fits. If every
    listed vendor is on two-day and it still does not fit, the plan says so and
    the budget is NOT raised -- that is a cadence decision (PRD §2.9).
    """
    runs_per_day = FETCH_RUNS_PER_DAY if runs_per_day is None else runs_per_day
    max_boards = STARTUP_RUN_MAX_BOARDS if max_boards is None else max_boards
    order = TWO_DAY_VENDOR_ORDER if order is None else order
    slots = 2 * runs_per_day
    hashes = [(b["ats"], board_hash(b["ats"], b["slug"])) for b in boards]

    def largest(two_day):
        loads = [0] * slots
        for ats, h in hashes:
            if ats in two_day:
                loads[h % slots] += 1
            else:
                for s in range(slots):
                    if h % runs_per_day == s % runs_per_day:
                        loads[s] += 1
        return max(loads) if loads else 0

    two_day = []
    size = largest(two_day)
    present = {ats for ats, _ in hashes}
    for vendor in order:
        if size <= max_boards:
            break
        if vendor not in present:
            continue
        two_day.append(vendor)
        size = largest(two_day)
    return two_day, size


def cadence_note(two_day, largest, max_boards=None):
    """The planner's verdict as log lines: the cadence, and OVER CAPACITY when
    the largest run exceeds STARTUP_RUN_MAX_BOARDS with every listed vendor
    already on two-day."""
    cap = STARTUP_RUN_MAX_BOARDS if max_boards is None else max_boards
    lines = [f"[Startup] cadence: two-day vendors: {', '.join(two_day)}; all others daily "
             f"(largest run {largest} of {cap} boards)" if two_day else
             f"[Startup] cadence: all vendors daily (largest run {largest} of {cap} boards)"]
    if largest > cap:
        lines.append(f"[Startup] OVER CAPACITY: largest run {largest} > {cap} boards with every listed "
                     f"vendor on two-day — cadence decision needed; the budget is not raised")
    return lines


class FetchResult(list):
    """The tier's jobs, plus whether every selected board was attempted.

    complete is False when the budget ran out (boards skipped or abandoned).
    A sharded fetch run records its shard as done only when complete, so an
    incomplete shard is taken again by the next fetch slot.
    """

    def __init__(self, jobs=(), complete=True, boards=0):
        super().__init__(jobs)
        self.complete = complete
        self.boards = boards


def fetch_all(budget_secs=None, workers=None, slot=None, runs_per_day=None, max_boards=None):
    """Fetch the startup tier, or the boards due in one cycle slot when slot is given."""
    budget_secs = STARTUP_FETCH_BUDGET_SECS if budget_secs is None else budget_secs
    workers = STARTUP_FETCH_WORKERS if workers is None else workers
    runs_per_day = FETCH_RUNS_PER_DAY if runs_per_day is None else runs_per_day
    boards = load_boards()
    if slot is not None:
        total = len(boards)
        two_day, largest = plan_cadence(boards, runs_per_day, max_boards)
        for line in cadence_note(two_day, largest, max_boards):
            print(line)
        unlisted = sorted({b["ats"] for b in boards} - set(TWO_DAY_VENDOR_ORDER))
        if unlisted:
            print(f"[Startup] vendors missing from config.TWO_DAY_VENDOR_ORDER (always daily): {', '.join(unlisted)}")
        boards = [b for b in boards if in_slot(b, slot, runs_per_day, set(two_day))]
        print(f"[Startup] slot {slot}/{2 * runs_per_day}: {len(boards)} of {total} boards")
    if not boards:
        return FetchResult()

    start = time.monotonic()
    deadline = start + budget_secs
    todo = queue.Queue()
    for board in boards:
        todo.put(board)
    done = queue.Queue()

    def worker():
        while True:
            try:
                board = todo.get_nowait()
            except queue.Empty:
                return
            if time.monotonic() >= deadline:
                done.put(("skipped", board, None))
                continue
            try:
                batch = fetch_one_board(board["company"], board["ats"], board["slug"],
                                        board["board_url"], source="startups")
                error = getattr(batch, "error", None)
                if error is not None:
                    done.put(("error", board, error))
                else:
                    done.put(("ok", board, batch))
            except Exception as e:
                done.put(("error", board, e))
            time.sleep(BOARD_DELAY)  # courtesy delay, per worker

    threads = [threading.Thread(target=worker, daemon=True, name=f"startup-{i}")
               for i in range(min(workers, len(boards)))]
    for t in threads:
        t.start()

    jobs = []
    stats = defaultdict(Counter)  # ats -> boards / ok / errored / skipped / jobs
    for b in boards:
        stats[b["ats"]]["boards"] += 1
    failures = Counter()          # (ats, status) -> boards
    collected = 0
    hard_stop = deadline + IN_FLIGHT_GRACE_SECS
    while collected < len(boards):
        try:
            status, board, payload = done.get(timeout=max(0.1, hard_stop - time.monotonic()))
        except queue.Empty:
            break  # hard stop: whatever is still in flight is abandoned
        collected += 1
        vendor = stats[board["ats"]]
        if status == "ok":
            vendor["ok"] += 1
            vendor["jobs"] += len(payload)
            vendor["postings"] += getattr(payload, "postings", None) or 0
            for job in payload:
                # Tag only. is_dream_company stays False so scorer.filter_jobs
                # applies the normal thresholds and the dream tier keeps its
                # score>=3 shortcut to the main section.
                job["source"] = "startups"
                job["tier"] = "startup"
                job["is_dream_company"] = False
            jobs.extend(payload)
        elif status == "error":
            vendor["errored"] += 1
            kind = _error_kind(payload)
            failures[(board["ats"], kind)] += 1
            print(f"[Startup] FAILED {board['company']} ({board['ats']}/{board['slug']}) "
                  f"[{kind}]: {payload}")
        else:
            vendor["skipped"] += 1

    # Boards never picked up before the hard stop count as skipped; boards a
    # worker was still fetching at the hard stop are abandoned.
    while True:
        try:
            stats[todo.get_nowait()["ats"]]["skipped"] += 1
        except queue.Empty:
            break
    for vendor in stats.values():
        vendor["abandoned"] = vendor["boards"] - vendor["ok"] - vendor["errored"] - vendor["skipped"]

    _report(stats, failures, len(boards), time.monotonic() - start, budget_secs, len(threads))
    unfinished = sum(v["skipped"] + v["abandoned"] for v in stats.values())
    return FetchResult(jobs, complete=unfinished == 0, boards=len(boards))


def _report(stats, failures, total, elapsed, budget_secs, n_workers):
    tot = Counter()
    for vendor in stats.values():
        tot.update(vendor)
    attempted = tot["ok"] + tot["errored"]
    parked = Counter(a for a, _ in read_parked())
    budget_note = ("BUDGET EXHAUSTED" if tot["skipped"] or tot["abandoned"] else "within budget")
    print(f"[Startup] boards: {total} total, {attempted} attempted, {tot['ok']} succeeded, "
          f"{tot['errored']} failed, {tot['skipped']} skipped, {tot['abandoned']} abandoned in flight "
          f"— {elapsed:.0f}s of {budget_secs}s budget, {n_workers} workers ({budget_note})")
    rate = 100 * tot["ok"] / attempted if attempted else 0.0
    print(f"[Startup] board success rate: {rate:.1f}% ({tot['ok']}/{attempted} attempted)")
    print(f"[Startup] per vendor:   {'vendor':<16}{'boards':>7}{'ok':>6}{'failed':>7}"
          f"{'skipped':>8}{'parked':>7}{'jobs':>6}")
    for ats in sorted(stats, key=lambda a: -stats[a]["boards"]):
        v = stats[ats]
        print(f"[Startup] per vendor:   {ats:<16}{v['boards']:>7}{v['ok']:>6}{v['errored']:>7}"
              f"{v['skipped'] + v['abandoned']:>8}{parked.get(ats, 0):>7}{v['jobs']:>6}")
    if failures:
        print("[Startup] failure inventory (vendor / status: boards, share of all boards):")
        for (ats, kind), n in sorted(failures.items(), key=lambda kv: -kv[1]):
            print(f"[Startup]   {ats} / {kind}: {n} ({100 * n / total:.1f}%)")
    # Log only: tells an empty board from a vendor serving the runner nothing.
    for ats in sorted(stats, key=lambda a: -stats[a]["boards"]):
        print(postings_line("[Startup]", ats, stats[ats]["postings"], stats[ats]["jobs"]))
    print(f"[Startup] {tot['jobs']} title-matched roles from {tot['ok']} boards")
