# main.py — Job Digest Agent orchestrator

import json
import os
import sys
import threading
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from datetime import datetime

# PUB-01/PUB-06: help, profile-check, cron, slot, doctor, setup, calibrate and
# digest --dry-run need no secrets (or only their own), so cli.py runs them
# before the pipeline imports below (scorer.py and some sources read their API
# keys at import time). It also applies the PUB-08 --scheduled slot guard.
if __name__ == "__main__":
    # PUB-10: a local .env (gitignored) fills in what the shell has not set,
    # before anything below reads the environment.
    import envfile
    envfile.load()
    import cli
    _code = cli.early(sys.argv[1:])
    if _code is not None:
        sys.exit(_code)

import importlib

import pending
import state
from llm import ApiAccessError
from state import board_id, content_key, normalize_url, same_posting
# OPS-12 / REV-02: main.FAILED_FETCH_CONCLUSIONS and main.load_failed_fetch_runs stay importable here.
from state import FAILED_FETCH_CONCLUSIONS, load_failed_fetch_runs  # noqa: F401
from scorer import score_jobs, filter_jobs, is_disqualified, is_hard_blocked, cap_calls
import sheet_retry
import fetch_budget
import scorer as scorer_module
from sheets import (append_jobs, append_disqualified_jobs, get_unseen_jobs, get_declined_feedback,
                    write_rows as write_sheet_rows, row_dicts as sheet_row_dicts,
                    check_tabs as check_sheet_tabs,
                    get_recent_index_rows)
from emailer import send_digest, send_alert_only, ALERT_NO_MATCHES_NOTE, SHEET_NO_MATCHES_NOTE
import profile_loader
from config import (
    JOB_BOARD_COMPANY_BLOCKLIST, TITLE_GATED_SOURCES, FETCH_BUDGET_SECS,
    SOURCE_BUDGET_SECS, FETCH_CYCLE_SLOTS, DAILY_SOURCES, DAILY_SOURCE_SLOT_UTC, PENDING_DIR, EVERY_RUN_SOURCES,
    MAX_SCORING_DEFERRALS, HIRING_COLLAPSE_LOOKBACK_DAYS, title_gate, source_rank,
    SHEET_NAME, DISQUALIFIED_SHEET_NAME,
)
from location import (passes_location_filter, location_penalty, needs_relo,
                      log_location_resolutions)
from url_resolver import resolve_jobs
from sheet_dates import freshness_counts


def load_connections():
    """Warm-contact matching is optional (PUB-13): connections.py is the
    maintainer's and does not ship, so with it absent the match is off."""
    try:
        import connections
    except ImportError:
        print("[Connections] connections.py not found: warm contact match off")
        return {}
    return connections.load_connections()


def find_warm_contact(company, connections):
    if not connections:
        return ""
    import connections as matcher
    return matcher.find_warm_contact(company, connections)


# PUB-04: digest thresholds and caps (profile.yaml scoring).
_SCORING = profile_loader.active()["scoring"]
WORTH_A_LOOK = _SCORING["thresholds"]["worth_a_look"]

# Each source by its module key (config.SOURCE_DISPLAY_NAMES has the names the
# Sheet and digest show). Fetch order, not priority order. Which copy of a duplicate survives is
# config.SOURCE_PRIORITY (CLAUDE.md rule 6), applied by add() and by
# pending.dedupe, so a late-arriving ATS copy still displaces an early Adzuna
# one. Wellfound is parked (PRD §2.4): zero postings from runner IPs, so it is
# not wired here; sources/wellfound.py is kept for a re-probe.
#
# PUB-05: (key, module) in fetch order; below it becomes (key, fetch_all) for
# the sources the profile's sources.enabled turns on. A disabled source is
# never imported (several read their API keys at import) and never called.
SOURCES = [
    ("adzuna", "sources.adzuna"),
    ("jsearch", "sources.jsearch"),
    ("linkedin_email", "sources.linkedin_email"),
    ("builtin", "sources.builtin"),
    ("ats", "sources.ats"),
    ("watchlist", "sources.watchlist"),
    ("startups", "sources.startups"),
    ("bespoke", "sources.bespoke"),
    ("usajobs", "sources.usajobs"),
]


def enabled_sources(table, enabled):
    """(key, fetch_all) for each source in `table` that `enabled` turns on,
    importing only those. A disabled one logs one line; one enabled in the
    profile but not in the table (Wellfound, parked) logs that it is not wired."""
    out = []
    for key, module in table:
        if not enabled.get(key):
            print(f"[{key}] disabled in profile")
            continue
        out.append((key, importlib.import_module(module).fetch_all))
    for key in sorted(k for k, on in enabled.items() if on and k not in dict(table)):
        print(f"[{key}] enabled in profile but not wired into main.SOURCES (parked) — not called")
    return out


SOURCES = enabled_sources(SOURCES, profile_loader.active()["sources"]["enabled"])
# The startup tier is fetched per cycle slot (OPS-04), not through SOURCES.
fetch_startups = dict(SOURCES).get("startups")


def _enabled(names):
    """`names` (config.DAILY_SOURCES / EVERY_RUN_SOURCES) narrowed to the
    sources this run carries."""
    keys = {n for n, _ in SOURCES}
    return [n for n in names if n in keys]

# Every stage's wall-clock, printed as a table at the end of the run so the
# Actions log shows where the time went: (stage, seconds, note).
TIMINGS = []
# name -> True if the source returned normally this run. A daily source is
# recorded as done for the day only when it succeeded (OPS-04).
SOURCE_OK = {}
# (source, line) for a source that returned normally but wants a warning in
# the fetch summary -- Bespoke's api_errors stop (OPS-12).
SOURCE_WARNINGS = []


@contextmanager
def timed(stage):
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        TIMINGS.append((stage, elapsed, ""))
        print(f"[Timing] {stage}: {elapsed:.1f}s")


def run_source(name, fetch, fetch_deadline):
    """Fetch one source under its wall-clock budget.

    The run must complete and send even when a source is slow or unreachable
    (PRD §7). The fetch runs in a daemon thread; if it is still going when its
    budget (or the overall fetch budget) runs out, it is abandoned and the run
    continues without it. A source that raises is logged with its traceback
    and skipped rather than taking the whole digest down with it.
    """
    budget = min(SOURCE_BUDGET_SECS.get(name, 300), fetch_deadline - time.monotonic())
    if budget <= 0:
        print(f"[Budget] {name} not started — the {FETCH_BUDGET_SECS}s overall fetch budget is spent")
        TIMINGS.append((name, 0.0, "NOT STARTED, fetch budget spent"))
        SOURCE_OK[name] = False
        return []

    result = {}

    def target():
        try:
            result["jobs"] = fetch()
        except ApiAccessError as e:
            result["fatal"] = e
        except Exception:
            result["error"] = traceback.format_exc()

    start = time.monotonic()
    thread = threading.Thread(target=target, name=f"source-{name}", daemon=True)
    thread.start()
    thread.join(budget)
    elapsed = time.monotonic() - start

    if thread.is_alive():
        # A source that can be told to stop is told now, so its thread cannot
        # go on to save state for results this run has already dropped.
        if callable(getattr(fetch, "cancel", None)):
            fetch.cancel()
        print(f"[Budget] {name} still running after {budget:.0f}s — abandoned, continuing without it")
        TIMINGS.append((name, elapsed, "BUDGET EXCEEDED, results dropped"))
        SOURCE_OK[name] = False
        return []
    if "fatal" in result:
        # OPS-12: no API credit or a bad key is not this source's problem; it
        # ends the whole run before anything is queued or marked seen.
        TIMINGS.append((name, elapsed, "API ACCESS ERROR, run aborted"))
        raise result["fatal"]
    if "error" in result:
        print(f"[{name}] SOURCE FAILED after {elapsed:.1f}s — continuing without it\n{result['error']}")
        TIMINGS.append((name, elapsed, "FAILED, see traceback above"))
        SOURCE_OK[name] = False
        return []
    # Not `or []`: an empty FetchResult still carries whether the startup shard
    # was complete, and a plain list would read as complete.
    jobs = result.get("jobs")
    if jobs is None:
        jobs = []
    SOURCE_OK[name] = True
    if getattr(jobs, "warning", ""):
        SOURCE_WARNINGS.append((name, jobs.warning))
    print(f"[Timing] {name}: {elapsed:.1f}s, {len(jobs)} jobs returned")
    TIMINGS.append((name, elapsed, f"{len(jobs)} jobs"))
    return jobs


def print_timing_summary(run_start):
    print("=== Timing summary ===")
    for stage, secs, note in TIMINGS:
        print(f"  {stage:<26} {secs:8.1f}s  {note}")
    print(f"  {'TOTAL':<26} {time.monotonic() - run_start:8.1f}s")


MODES = ("fetch", "digest", "all")


class SheetWriteError(RuntimeError):
    """A digest Sheet write (or a REV-03 retry of one) failed; the email still
    went and the rows wait in state/sheet_retry/ (TRK-06). Exits non-zero."""


def main(mode="all", force_daily=False):
    run_start = time.monotonic()
    try:
        if mode == "fetch":
            fetch_run(force_daily=force_daily)
        elif mode == "digest":
            digest_run()
        else:
            # Standalone local run: every source, the whole startup tier, then
            # the digest -- the pre-OPS-04 single-run behaviour.
            fetch_run(shard=None)
            digest_run()
    finally:
        print_timing_summary(run_start)


def _pick_fetch_plan(shard_state, shard, force_daily=False):
    """Which startup cycle slot and which daily sources this fetch run takes."""
    if shard == "auto":
        shard = state.next_shard(shard_state)
    if shard is None or force_daily:
        daily = _enabled(DAILY_SOURCES)
    else:
        daily = [name for name in _enabled(DAILY_SOURCES)
                 if state.daily_source_due(shard_state, name, DAILY_SOURCE_SLOT_UTC)]
    return shard, daily


def fetch_run(shard="auto", force_daily=False):
    """Fetch, dedup, score, and queue new postings for the digest (OPS-04).

    shard="auto" takes the startup cycle slot with the oldest last success;
    shard=None fetches the whole tier and every source (local standalone run).
    force_daily runs every daily source regardless of the 04:17 UTC slot rule
    (a manual dispatch of fetch.yml). Nothing here writes the Sheet or sends email.
    """
    print("=== Job Digest Agent: fetch run ===")
    fetch_budget.reset_run()                 # REV-06: this run's model-call tally
    found_at = datetime.utcnow()
    print(f"Time: {found_at.strftime('%Y-%m-%d %H:%M UTC')}")

    first_run = state.is_first_run()
    print(f"First run: {first_run}")

    shard_state = state.load_shard_state(FETCH_CYCLE_SLOTS)
    shard, daily = _pick_fetch_plan(shard_state, shard, force_daily)
    if shard is None:
        print(f"[Plan] whole startup tier; all sources: {', '.join(_enabled(DAILY_SOURCES))}")
    else:
        skipped = [n for n in _enabled(DAILY_SOURCES) if n not in daily]
        print(f"[Plan] startup slot {shard}/{FETCH_CYCLE_SLOTS} "
              f"(last success: {shard_state['last_success'].get(str(shard), 'never')}); "
              f"daily sources due: {', '.join(daily) or 'none'}"
              f"{'; already fetched since %02d:%02d UTC: %s' % (*DAILY_SOURCE_SLOT_UTC, ', '.join(skipped)) if skipped else ''}"
              f"{' (forced: manual dispatch)' if force_daily else ''}")

    def startup_fetch():
        if shard is None:
            return fetch_startups()
        return fetch_startups(slot=shard)

    run_sources = [(name, fetch) for name, fetch in SOURCES
                   if name in daily or (name in _enabled(EVERY_RUN_SOURCES) and name != "startups")]
    if any(name == "startups" for name, _ in SOURCES):
        run_sources.append(("startups", startup_fetch))
    run_sources.sort(key=lambda s: [n for n, _ in SOURCES].index(s[0]))

    SOURCE_WARNINGS.clear()
    state.purge_old_entries()
    connections = load_connections()

    all_jobs = []
    seen_urls = set()
    # FIL-10 second key, checked before scoring so a repeat costs no Claude
    # call. Three things seed it, all in the same shape
    # {content key: [(host, posting id), ...]}:
    #   - content keys persisted by earlier fetch runs (seen_jobs.json), which
    #     is what stops an Adzuna ad re-served daily under a rotating `se=`
    #     token, and a Dragos copy arriving a week after the first;
    #   - postings already queued for a digest that has not gone out yet;
    #   - whatever this run has added so far.
    # A key already carrying a posting on the same host under a different id
    # does NOT match (state.same_posting): MITRE R117090 and R116664 are two
    # requisitions, not one posting seen twice.
    persisted_content = state.load_content_index()
    seen_content = {}
    for key, idents in pending.content_keys().items():
        seen_content[key] = list(idents)
    # index of this run's kept copy per content key, for the priority contest
    kept_by_content = {}
    content_dropped = Counter()
    # FIL-12: persisted keys a dropped repeat matched, with the repeat's URL, so
    # the key's retention window restarts from this sighting.
    content_restamp = {}
    funnel = Counter()
    startup_result = None

    _blocklist_lower = {b.lower() for b in JOB_BOARD_COMPANY_BLOCKLIST}

    def _keep_better_copy(job, key, url):
        """A second copy of a posting this run already kept: keep whichever
        source ranks higher (CLAUDE.md rule 6), so the stored apply link is the
        best one. Either way the run carries one row, not two. Nothing is
        scored yet, so there is no rationale to choose between here — that
        contest is pending.dedupe's, across fetch runs."""
        kept = kept_by_content.get(key)
        if kept is None or source_rank(job.get("source", "")) >= source_rank(kept.get("source", "")):
            return
        all_jobs[all_jobs.index(kept)] = job
        kept_by_content[key] = job
        if url:
            seen_urls.add(url)
        print(f"[Dedup] {job.get('source')} copy of {job.get('title', '')!r} at "
              f"{job.get('company', '')!r} replaces the {kept.get('source')} one "
              f"(higher apply-link priority)")

    def add(batch, name):
        gated = name in TITLE_GATED_SOURCES
        count = drop_title = drop_blocklist = drop_url_dedup = drop_content = 0
        for job in batch:
            title = (job.get("title") or "").strip()
            # Title gate first (PRD §3), so a gated-out job never claims a URL
            # or content key that a later source's copy could use.
            if gated and not title_gate(title):
                drop_title += 1
                continue
            company = (job.get("company") or "").strip()
            if company.lower() in _blocklist_lower:
                drop_blocklist += 1
                continue
            raw_url = job.get("apply_link", "").strip()
            url = normalize_url(raw_url)
            if url:
                job["apply_link"] = url  # store normalized URL
            # First key: the tracking-stripped URL (FIL-03).
            if url and url in seen_urls:
                drop_url_dedup += 1
                continue
            # Second key: normalised company + title + location (FIL-10),
            # against earlier runs and this one, before scoring.
            key = content_key(job)
            if key:
                persisted = persisted_content.get(state.content_hash(key))
                if persisted is not None and same_posting(persisted, url):
                    drop_content += 1
                    content_dropped[name] += 1
                    content_restamp.setdefault(key, []).append(url)
                    print(f"[Dedup] {name}: {job.get('title', '')!r} at "
                          f"{job.get('company', '')!r} already seen in an earlier run")
                    continue
                idents = seen_content.get(key)
                if idents is not None and same_posting(idents, url):
                    drop_content += 1
                    content_dropped[name] += 1
                    _keep_better_copy(job, key, url)
                    ident = board_id(url)
                    if ident and ident not in idents:
                        idents.append(ident)
                    continue
            if url:
                seen_urls.add(url)
            if key:
                idents = seen_content.setdefault(key, [])
                ident = board_id(url)
                if ident and ident not in idents:
                    idents.append(ident)
                kept_by_content.setdefault(key, job)
            all_jobs.append(job)
            count += 1
        dated, fresh = freshness_counts(batch)
        total_in = count + drop_title + drop_blocklist + drop_url_dedup + drop_content
        funnel["fetched"] += total_in
        funnel["title_gate"] += drop_title
        funnel["blocklist_dedup"] += drop_blocklist + drop_url_dedup + drop_content
        print(f"[{name}] fetched={total_in}  added={count}  "
              f"dropped: title_gate={drop_title} blocklist={drop_blocklist} "
              f"url_dedup={drop_url_dedup} content_dedup={drop_content}"
              f"{'' if gated else '  (title gate exempt)'}")
        print(f"[{name}] posted date: {dated}/{len(batch)} roles  fresh: {fresh}")

    fetch_deadline = time.monotonic() + FETCH_BUDGET_SECS
    commits = []  # sources whose state may be saved only once their jobs are queued
    source_summaries = []     # REV-07: a source's own counts for the fetch summary
    for name, fetch in run_sources:
        batch = run_source(name, fetch, fetch_deadline)
        if name == "startups":
            startup_result = batch
        if getattr(batch, "summary", ""):
            source_summaries.append((name, batch.summary))
        if SOURCE_OK.get(name) and callable(getattr(batch, "commit", None)):
            commits.append((name, batch.commit))
        add(batch, name)

    # REV-07: LinkedIn jobs whose page failed LINKEDIN_MAX_PAGE_FAILURES runs
    # in a row skip location, the cap and the model: recorded at score 0 below.
    unavailable = [j for j in all_jobs if j.get("unavailable_reason")]
    all_jobs = [j for j in all_jobs if not j.get("unavailable_reason")]
    before_filter = len(all_jobs)
    gated_jobs = list(all_jobs)
    all_jobs = [j for j in all_jobs if passes_location_filter(j)]
    # Provisional relo penalty/tag from whatever work_model the source parser gave
    # us. Most ATS APIs never expose a real hybrid/onsite signal, so this is often
    # wrong (blank); scorer.score_jobs recomputes both once Claude has read the JD
    # and corrected work_model (FIL-08), before anything renders it.
    for job in all_jobs:
        job["location_penalty"] = location_penalty(job)
        job["relo"] = needs_relo(job)
    dropped_loc = before_filter - len(all_jobs)
    print(f"Total unique jobs after location filter: {len(all_jobs)} (dropped {dropped_loc})")

    # Per-source location filter breakdown
    src_counts = Counter(j.get("source", "?") for j in all_jobs)
    print(f"  By source after location filter: { {s: src_counts[s] for s in sorted(src_counts)} }")

    if first_run:
        new_jobs = all_jobs
    else:
        # One read of seen_jobs.json for the whole batch, not one per posting.
        url_index = state.load_url_index()
        new_jobs = [j for j in all_jobs
                    if state.job_hash(j.get("apply_link", "")) not in url_index]

    seen_dropped = len(all_jobs) - len(new_jobs)
    print(f"New jobs to score: {len(new_jobs)} (dropped {seen_dropped} already-seen)")

    # PUB-12: at most scoring.max_calls_per_run scoring calls. Postings past the
    # cap are held like an OPS-12 deferral (no count): not scored, not queued,
    # not marked seen, so the next run that serves them scores them.
    # REV-06: and at most what is left of scoring.max_calls_per_day today
    # (delivery.timezone), after this run's Bespoke extractions. Postings past
    # the ceiling are held the same way, dream companies first.
    cap = profile_loader.active()["scoring"]["max_calls_per_run"]
    daily_ceiling = fetch_budget.ceiling()
    daily_left = fetch_budget.remaining_today()
    by_ceiling = daily_left < cap
    new_jobs, held = cap_calls(new_jobs, min(cap, daily_left), profile_loader.dream_companies())
    held_by_ceiling = len(held) if by_ceiling else 0
    if held and by_ceiling:
        line = (f"daily scoring ceiling reached: {len(held)} postings held unseen for a later run "
                f"(scoring.max_calls_per_day {daily_ceiling}, {daily_left} calls were left today "
                f"{fetch_budget.today()})")
        print(f"[Scorer] {line}")
        SOURCE_WARNINGS.append(("scorer", line))
    elif held:
        print(f"[Scorer] cap reached: {len(held)} postings held for the next run "
              f"(scoring.max_calls_per_run {cap})")
        SOURCE_WARNINGS.append(("scorer", f"cap reached: {len(held)} postings held for the next run "
                                          f"(scoring.max_calls_per_run {cap})"))

    # Per-source seen-jobs breakdown
    src_new = Counter(j.get("source", "?") for j in new_jobs)
    print(f"  By source after seen_jobs filter: { {s: src_new[s] for s in sorted(src_new)} }")

    # Capture pre-resolution URLs for state tracking — resolve_jobs mutates apply_link
    # in place to the resolved company URL, but is_seen was checked against the original
    # aggregator URL. We must mark_seen using the same URL format that was checked.
    urls_to_mark_seen = [j.get("apply_link", "") for j in new_jobs if j.get("apply_link")]
    seen_url_of = {id(j): j.get("apply_link", "") for j in new_jobs}
    # FIL-10: the same postings' content keys, with the URL each arrived as.
    # The resolved URL is added after resolve_jobs, so a later copy is matched
    # against both the aggregator link and the direct one it resolved to.
    content_to_mark_seen = {}
    for job in new_jobs:
        key = content_key(job)
        if key:
            content_to_mark_seen.setdefault(key, []).append(job.get("apply_link", ""))

    # Preserve canonical LinkedIn posting URLs before resolution mutates
    # apply_link — the digest renders these as a second "LinkedIn" link.
    # Only real /jobs/view/<id> URLs count; never constructed otherwise.
    import re as _re
    for job in new_jobs:
        m = _re.search(r"linkedin\.com(?:/comm)?/jobs/view/(\d+)", job.get("apply_link", ""))
        if m:
            job["linkedin_url"] = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"

    print("Resolving apply links...")
    with timed("Resolve apply links"):
        resolve_jobs(new_jobs)

    for job in new_jobs:
        key = content_key(job)
        if key and key in content_to_mark_seen:
            content_to_mark_seen[key].append(job.get("apply_link", ""))

    for job in new_jobs:
        job["warm_contact"] = find_warm_contact(job.get("company", ""), connections)

    # A billing/auth/permission error raises ApiAccessError out of score_jobs
    # (OPS-12), so the run ends here: nothing below queues or marks seen.
    with timed("Scoring"):
        declined_feedback = get_declined_feedback() if new_jobs else []
        print(f"Loaded {len(declined_feedback)} declined-role feedback entries")
        calls_before = dict(scorer_module.USAGE)
        scored = score_jobs(new_jobs, declined_feedback=declined_feedback) if new_jobs else []

    # OPS-12: a scoring call that failed on a transient API error after the
    # SDK's retries is deferred: not queued, not marked seen, so the next run
    # that serves the posting scores it again. After MAX_SCORING_DEFERRALS it
    # is recorded at score 0 "Scoring error", as before.
    deferrals = state.load_deferrals()
    deferral_counts, deferral_cleared, deferred = {}, [], []
    for job in scored:
        url = seen_url_of.get(id(job), "")
        dkey = state.deferral_key(url) if url else None
        error = job.pop("scoring_error", "")
        count = deferrals.get(dkey, 0)
        if error and dkey and count < MAX_SCORING_DEFERRALS:
            deferral_counts[dkey] = count + 1
            deferred.append(job)
            print(f"[Scorer] deferred: {job.get('title', '')} at {job.get('company', '')} ({error})"
                  f" — attempt {count + 1} of {MAX_SCORING_DEFERRALS}, retried next run")
            continue
        if error:
            print(f"[Scorer] recorded at score 0 after {count} deferrals: "
                  f"{job.get('title', '')} at {job.get('company', '')} ({error})")
        if dkey in deferrals:
            deferral_cleared.append(dkey)
    if deferred:
        deferred_ids = {id(j) for j in deferred}
        scored = [j for j in scored if id(j) not in deferred_ids]
        deferred_urls = {seen_url_of[id(j)] for j in deferred}
        urls_to_mark_seen = [u for u in urls_to_mark_seen if u not in deferred_urls]
        for job in deferred:
            content_to_mark_seen.pop(content_key(job), None)
        print(f"[Scorer] {len(deferred)} postings deferred to a later run (transient API errors)")
    # How each scored hybrid/onsite location was placed (FIL-09): one line per run.
    log_location_resolutions()

    # FIL-13: Company holds the employer, "<hiring company> (via <poster>)"
    # when the scoring call named one. After every content key above was taken
    # on the posted name, which posted_company keeps.
    named = sum(state.name_employer(job) for job in scored)
    print(f"[Employer] {named} postings named for the employer (Company = '<employer> (via <poster>)')")

    if unavailable:
        for job in unavailable:
            job.update(score=0, base_score=0, rationale=job["unavailable_reason"], hard_block_industry=None,
                       is_cool_company_wrong_role=False)
            if job.get("apply_link"):
                urls_to_mark_seen.append(job["apply_link"])
        scored = scored + unavailable
        print(f"[LinkedIn Email] {len(unavailable)} jobs recorded at score 0 as "
              f"'{unavailable[0]['unavailable_reason']}' (Disqualified tab)")

    # Score distribution
    if scored:
        score_dist = Counter(j.get("score", 0) for j in scored)
        print(f"Score distribution: { dict(sorted(score_dist.items())) }")
        li_scored = [j for j in scored if j.get("source") == "linkedin_email"]
        if li_scored:
            li_dist = Counter(j.get("score", 0) for j in li_scored)
            print(f"  LinkedIn scores: { dict(sorted(li_dist.items())) }")
            print(f"  LinkedIn score>0: {sum(1 for j in li_scored if j.get('score',0) > 0)}  score=0: {sum(1 for j in li_scored if j.get('score',0) == 0)}")
        good = Counter(j.get("source", "?") for j in scored
                       if j.get("score", 0) >= WORTH_A_LOOK)
        print(f"  By source score>={WORTH_A_LOOK}: { {s: good[s] for s in sorted(good)} }")

    # FIL-10: what the second key kept out of scoring, by source.
    print(f"[Dedup] content key (FIL-10) dropped {sum(content_dropped.values())} "
          f"postings before scoring: { {s: content_dropped[s] for s in sorted(content_dropped)} or 'none' }"
          f"  (persisted keys loaded: {len(persisted_content)})")

    score_zero = sum(1 for j in scored if j.get("score", 0) == 0)
    print("=== Funnel (this fetch run) ===")
    print(f"  fetched {funnel['fetched']} → title gate −{funnel['title_gate']} → "
          f"blocklist/dedup −{funnel['blocklist_dedup']} → {before_filter} → "
          f"location −{dropped_loc} → {len(all_jobs)} → already seen −{seen_dropped} → "
          f"{f'held by the scoring cap −{len(held)} → ' if held else ''}"
          f"new {len(new_jobs)} → scored {len(scored)} (score 0: {score_zero}) → queued for the digest")

    # Direct ATS fetching is the core of the pipeline; aggregators supplement
    # it (PRD §2). This line is the number to watch for that balance.
    print("=== Channel share (this fetch run) ===")
    print(f"  after gate+dedup:     {_by_channel(gated_jobs)}")
    print(f"  after location:       {_by_channel(all_jobs)}")
    print(f"  new (scored):         {_by_channel(new_jobs)}")
    print(f"  score >= {WORTH_A_LOOK}:          "
          f"{_by_channel([j for j in scored if j.get('score', 0) >= WORTH_A_LOOK])}")

    # Persist in crash-safe order. Queue first: if the run dies after this but
    # before seen-state is saved, the next run re-scores and re-queues the same
    # postings, and the digest's dedup sends them once. Seen-state first would
    # instead lose them for good.
    for job in scored:
        job["found_at"] = found_at.isoformat()
    label = "all" if shard is None else f"slot{shard}of{FETCH_CYCLE_SLOTS}"
    path = pending.write_batch(scored, label, found_at=found_at, first_run=first_run)
    print(f"[Queue] {len(scored)} scored postings queued for the digest"
          f"{f' -> {PENDING_DIR}/{path.name}' if path else ''}")
    state.mark_seen_batch(urls_to_mark_seen)
    # FIL-12: only keys already persisted are re-stamped. A key matched only
    # against this run or the queue belongs to a posting that is either written
    # below or (deferred) deliberately left unwritten.
    restamped = 0
    for key, urls in content_restamp.items():
        if key not in content_to_mark_seen:
            restamped += 1
        content_to_mark_seen.setdefault(key, []).extend(urls)
    marked = state.mark_content_seen_batch(list(content_to_mark_seen.items()), when=found_at)
    print(f"[Dedup] persisted {marked} content keys for later runs "
          f"({restamped} re-stamped by a dropped repeat, FIL-12)")
    state.save_deferrals(deferral_counts, deferral_cleared, when=found_at)
    # REV-06: this run's model calls toward today's ceiling. API calls
    # (retries included), and never fewer than the postings sent to scoring.
    sent_to_scoring = sum(1 for j in new_jobs if not scorer_module._is_hard_disqualify(j))
    scoring_calls = max(scorer_module.USAGE["calls"] - calls_before["calls"], sent_to_scoring) if new_jobs else 0
    day = fetch_budget.record(scoring_calls, fetch_budget.RUN["extractions"], held_by_ceiling)
    print(f"[Budget] {fetch_budget.today()}: {scoring_calls} scoring + {fetch_budget.RUN['extractions']} "
          f"extraction calls this run; {day['scoring_calls'] + day['extractions']} of "
          f"{daily_ceiling} today (scoring.max_calls_per_day)")
    # Source state that records pages as read (Bespoke) is saved only now,
    # after the postings from those pages are queued.
    for name, commit in commits:
        commit()

    if shard is not None:
        if SOURCE_OK.get("startups") and getattr(startup_result, "complete", False):
            state.record_shard_success(shard_state, shard, found_at)
            print(f"[Plan] slot {shard}/{FETCH_CYCLE_SLOTS} complete")
        else:
            print(f"[Plan] slot {shard}/{FETCH_CYCLE_SLOTS} NOT complete — the next fetch run takes it again")
        for name in daily:
            if SOURCE_OK.get(name):
                state.record_daily_source_success(shard_state, name, found_at)
            else:
                print(f"[Plan] daily source {name} did not succeed — due again on the next fetch run")
        state.save_shard_state(shard_state)

    # REV-06: scoring tokens, cached vs uncached, and what they cost.
    run_usage = {k: scorer_module.USAGE[k] - calls_before.get(k, 0) for k in scorer_module.USAGE}
    print(f"=== Scoring cost (this fetch run) ===\n  {scorer_module.usage_line(run_usage)}")
    if source_summaries:
        print("=== Source counts (this fetch run) ===")
        for name, line in source_summaries:
            print(f"  {name}: {line}")

    if SOURCE_WARNINGS:
        print("=== Warnings (this fetch run) ===")
        for name, line in SOURCE_WARNINGS:
            print(f"  {name}: {line}")
    print("=== Fetch run done ===")


def _by_channel(jobs):
    channels = {"direct ATS": {"ats", "startups", "watchlist"},
                "aggregator": {"adzuna", "jsearch", "builtin"},
                "LinkedIn alerts": {"linkedin_email"}}
    counts = Counter(next((c for c, srcs in channels.items() if j.get("source") in srcs), "other")
                     for j in jobs)
    return ", ".join(f"{c} {counts[c]}" for c in channels)


def run_inbox_readers(sources):
    """PUB-10: the Applied-seed and status-tracking passes, each only when the
    profile's sources.applied_doc / sources.status_tracking turns it on.
    REV-03: both work on rows already on the Sheet, so a posting still waiting
    in state/sheet_retry/ gets no status update until its row is written."""
    waiting = len(sheet_retry.waiting_rows())
    if waiting:
        print(f"[SheetRetry] {waiting} postings waiting in state/sheet_retry/ are not on the Sheet yet: "
              f"status updates for them are skipped until they are written")
    if sources["applied_doc"]["enabled"]:
        with timed("Applied-seed (§16)"):
            try:
                from sources.applied_doc import seed_from_doc
                seed_from_doc()
            except Exception as e:
                print(f"[AppliedDoc] Applied-seed failed (digest unaffected): {e}")
    else:
        print("[AppliedDoc] sources.applied_doc.enabled is false: Applied-seed skipped")
    if sources["status_tracking"]["enabled"]:
        with timed("Status tracking (§15)"):
            try:
                from sources.status_email import check_status_emails
                check_status_emails()
            except Exception as e:
                print(f"[StatusEmail] Status tracking failed (digest unaffected): {e}")
    else:
        print("[StatusEmail] sources.status_tracking.enabled is false: status tracking skipped")


def digest_run():
    """Email every queued posting once, log it to the Sheet, then dequeue it (OPS-04).

    Same sections, format, thresholds and empty-day behaviour as the
    pre-OPS-04 single run; only the postings now come from the queue that
    fetch runs fill.
    """
    print("=== Job Digest Agent: digest ===")
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    # REV-02: a run reported in an earlier sent email is not reported again.
    failed_fetch_runs = state.unreported_fetch_runs(load_failed_fetch_runs())
    # REV-03: rows earlier digests could not write go in first, oldest file
    # first, before anything reads the Sheet for this email.
    # REV-04: both tabs' header rows, read only, so a broken header shows even
    # on a day with nothing to write.
    with timed("Sheet header check (REV-04)"):
        header_problems = check_sheet_tabs()
    with timed("Sheet retry (REV-03)"):
        retry_errors = sheet_retry.replay(write_sheet_rows)
    paths, queued, first_run = pending.load_pending()
    # Batches queued before FIL-13's employer naming get it here; idempotent.
    named = sum(state.name_employer(job) for job in queued)
    print(f"[Employer] {named} queued postings named for the employer on load")
    scored = pending.dedupe(queued)
    print(f"[Queue] {len(paths)} fetch batches, {len(queued)} queued postings, "
          f"{len(scored)} after cross-batch dedup")
    # Batches queued before FIL-09 carry the old flat location penalty: re-tier them.
    retiered = pending.retier_legacy(scored)
    print(f"[Queue] {retiered} pre-FIL-09 postings re-tiered")
    if retiered:
        log_location_resolutions()
    sections = filter_jobs(scored)

    # FIL-13: the email shows one card per hiring company + title, across this
    # batch and recent Sheet rows. Read before this digest writes its own rows.
    # The Sheet still gets every row from `sections`.
    recent_rows = get_recent_index_rows(HIRING_COLLAPSE_LOOKBACK_DAYS) if scored else []
    email_jobs = pending.collapse_hiring(scored, recent_rows)
    email_sections = filter_jobs(email_jobs)
    print(f"[Collapse] {len(scored)} postings -> {len(email_jobs)} digest cards "
          f"(hiring company + title, against {len(recent_rows)} Sheet rows from the last "
          f"{HIRING_COLLAPSE_LOOKBACK_DAYS} days)")

    print("=== Digest ===")
    print(f"  queued {len(scored)} (score 0: {sum(1 for j in scored if j.get('score', 0) == 0)}) → "
          f"top matches {len(email_sections['main'])}, worth a look {len(email_sections['worth_a_look'])}, "
          f"cool company/wrong role {min(len(email_sections['cool_wrong_role']), _SCORING['cool_company_wrong_role_cap'])} "
          f"emailed of {len(email_sections['cool_wrong_role'])}")
    print(f"  by channel: {_by_channel(scored)}")

    # DEL-14: rewrite the emailed cards' blurbs from each company's own website
    # (cached per domain), before the Sheet write so the Sheet stores the same
    # text the card shows. Cool-company cards carry no blurb. Never blocks the
    # digest: any failure leaves the posting-only blurbs as they are.
    with timed("Company profiles (DEL-14)"):
        try:
            from profiles import enrich_blurbs
            cards = email_sections["main"] + email_sections["worth_a_look"]
            enrich_blurbs(cards)
            rewritten = {c["apply_link"]: c["rationale"] for c in cards
                         if c.get("profile_domain") and c.get("apply_link")}
            for job in scored:
                if job.get("apply_link") in rewritten:
                    job["rationale"] = rewritten[job["apply_link"]]
        except Exception as e:                       # noqa: BLE001
            print(f"[Profile] step failed, digest unaffected: {type(e).__name__}: {e}")

    print("Writing to Google Sheet...")
    # Score 0, or a hard-block industry the scoring call named at any score
    # (PUB-04), goes to the Disqualified tab.
    scored_fail = [j for j in sections["all_scored"] if j.get("score", 0) == 0 or is_disqualified(j)]
    fail_ids = {id(j) for j in scored_fail}
    scored_pass = [j for j in sections["all_scored"] if id(j) not in fail_ids]
    print(f"  → append_jobs (score>0):          {len(scored_pass)} jobs")
    print(f"  → append_disqualified_jobs (score=0 or hard block): {len(scored_fail)} jobs")
    sheet_errors = []
    with timed("Sheet writes"):
        # Both skip URLs already in the Sheet, so a digest retried after a
        # failure never writes a row twice. TRK-06: a failed write no longer
        # passes silently. The email still goes and the queue still drains,
        # then the run fails at the end so the red run shows in Actions.
        # REV-03: a failed write keeps its rows in state/sheet_retry/ (the
        # whole tab's rows; the replay skips any the write did get in).
        failures = []
        for name, write, tab, jobs in (
                ("append_jobs", append_jobs, SHEET_NAME, scored_pass),
                ("append_disqualified_jobs", append_disqualified_jobs, DISQUALIFIED_SHEET_NAME, scored_fail)):
            try:
                write(jobs)
            except Exception as e:
                msg = f"{name} failed for {len(jobs)} jobs: {type(e).__name__}: {e}"
                print(f"::error title=Sheet write failed::{msg}")
                sheet_errors.append(msg)
                failures.append((tab, sheet_row_dicts(tab, jobs), msg))
        sheet_retry.write_retry_file(failures)
    sheet_errors += retry_errors + [m for m in header_problems if m not in " ".join(sheet_errors)]
    # One banner line per header problem, then per retry file still waiting
    # (this run's included).
    sheet_notices = header_problems + sheet_retry.banner_lines()
    # REV-06: a day fetch hit the daily scoring ceiling, reported once
    # (fetch_budget.json is fetch's; the digest only reads it).
    try:
        events = fetch_budget.ceiling_events()
    except (OSError, ValueError) as e:
        print(f"::warning title=Fetch budget unreadable::{e} — no ceiling line in the banner")
        events = []
    new_ceiling_days = state.unreported_ceiling_days([day for day, _, _ in events])
    # REV-07: LinkedIn jobs recorded as unreadable in the last seven days.
    try:
        from datetime import timedelta
        week_start = (profile_loader.local_today() - timedelta(days=6)).isoformat()
        unreadable = fetch_budget.linkedin_unavailable_since(week_start)
    except (OSError, ValueError):
        unreadable = 0
    if unreadable:
        sheet_notices.append(
            f"{unreadable} LinkedIn job{'s' if unreadable != 1 else ''} could not be read this week: the page "
            f"was blocked or behind a login wall three runs running, so {'they are' if unreadable != 1 else 'it is'} "
            f"in the Disqualified tab as \"LinkedIn page unavailable\".")
    sheet_notices += [fetch_budget.banner_line(day, held, limit) for day, held, limit in events
                      if day in new_ceiling_days]
    sheet_alert_keys = [f"header:{m}" for m in header_problems] + sheet_retry.alert_keys()
    new_sheet_alerts = state.unreported_sheet_alerts(sheet_alert_keys)

    new_jobs = scored
    has_matches = bool(email_sections["main"] or email_sections["worth_a_look"]
                       or email_sections["cool_wrong_role"])
    # REV-02: failed or cancelled fetch runs always produce an email. With no
    # new postings it is the alert-only form, whatever skip_empty_days says;
    # otherwise the normal digest, which carries the same banner.
    # REV-04: a Sheet problem no email has reported yet (a header failure, a
    # retry file waiting) counts like a failed fetch run.
    alert_only = bool(failed_fetch_runs or new_sheet_alerts) and not first_run and not new_jobs
    if not first_run:
        # An alert-only day is still a skipped day: no postings went out.
        state.record_run_day(had_new_jobs=bool(new_jobs))

    sent = False
    with timed("Email"):
        if first_run:
            top_10 = sorted(email_jobs, key=lambda x: x.get("score", 0), reverse=True)[:10]
            send_digest(filter_jobs(top_10), is_first_run=True, failed_fetch_runs=failed_fetch_runs,
                        notices=sheet_notices)
            sent = True
            print("Sent first-run digest")
        elif alert_only:
            send_alert_only(failed_fetch_runs, notices=sheet_notices)
            sent = True
            state.record_alert_email()
            print(f"0 new jobs, {len(failed_fetch_runs)} failed fetch runs, {len(new_sheet_alerts)} new "
                  f"Sheet problems — sent the alert-only email (counted as a skipped day)")
        elif not new_jobs and profile_loader.active()["delivery"]["skip_empty_days"]:
            print("0 new jobs after dedup/filter — no daily email sent (counted toward weekly skipped-day total)")
        elif has_matches:
            send_digest(email_sections, failed_fetch_runs=failed_fetch_runs, notices=sheet_notices)
            sent = True
            print("Sent digest")
        else:
            fallback = get_unseen_jobs()
            if fallback:
                send_digest({"fallback_jobs": fallback}, is_fallback=True, failed_fetch_runs=failed_fetch_runs,
                            notices=sheet_notices)
                sent = True
                print("Sent fallback with {} jobs".format(len(fallback)))
            elif failed_fetch_runs or new_sheet_alerts:
                # New postings, none worth a card and nothing to resurface:
                # the failures still go out (REV-02, REV-04).
                send_alert_only(failed_fetch_runs, notices=sheet_notices,
                                note=ALERT_NO_MATCHES_NOTE if failed_fetch_runs else SHEET_NO_MATCHES_NOTE)
                sent = True
                state.record_alert_email()
                state.record_run_day(had_new_jobs=False)     # no postings went out
                print("No matches and no fallback — sent the alert-only email for the failed fetch runs")
            else:
                print("No matches and no fallback — skipping email")
    if sent:
        state.mark_fetch_runs_reported(failed_fetch_runs)
        state.mark_ceiling_days_reported(new_ceiling_days)
    state.mark_sheet_alerts_reported(sheet_alert_keys, sent)

    # Dequeue only after the email step returned. A send that raises leaves the
    # batches queued for the next digest instead of losing them.
    pending.clear(paths)
    print(f"[Queue] dequeued {len(paths)} batches")

    # Applied-seed from the index doc (PRD §16), then application-status
    # tracking (PRD §15) — seed first so status emails can promote seeded
    # rows in the same run. Both last so a failure never blocks the digest.
    run_inbox_readers(profile_loader.active()["sources"])

    if sheet_errors:
        raise SheetWriteError("; ".join(sheet_errors))
    print("=== Done ===")


if __name__ == "__main__":
    args = sys.argv[1:]
    force_daily = "--daily-sources" in args
    args = [a for a in args if a != "--daily-sources"]
    mode = args[0] if args else "all"
    if mode not in MODES or len(args) > 1 or (force_daily and mode != "fetch"):
        sys.exit(f"usage: python main.py [{'|'.join(MODES)}] [--daily-sources (fetch only)]  (default: all)\n"
                 f"       python main.py --help   for every command")
    try:
        main(mode, force_daily)
    except ApiAccessError as e:
        print(f"::error title=Anthropic API access failed::{e.cause} — fetch run aborted; "
              f"nothing was queued or marked seen, so the next run re-fetches these postings")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
