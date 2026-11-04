"""OPS-04: sharded fetch runs feed a digest that keeps its own schedule.

Covers: every board in exactly one shard; a failed or incomplete shard is taken
again by the next fetch slot; daily sources keep one successful fetch a day;
a posting found by any fetch run is emailed by the next digest exactly once;
a batch queued while a digest is running waits for the next digest; the
Sheet's Date First Seen is the fetch time. No network access.
"""

import importlib
import json
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "GOOGLE_SHEET_ID",
            "GOOGLE_SERVICE_ACCOUNT_JSON", "ADZUNA_APP_ID", "ADZUNA_API_KEY", "RAPIDAPI_KEY"):
    os.environ.setdefault(var, "test")

# The pipeline imports Google client libraries at module level; stub them when
# they are not installed locally, since nothing here talks to Google.
for name in ("gspread", "google", "google.oauth2", "google.oauth2.service_account",
             "googleapiclient", "googleapiclient.discovery"):
    try:
        importlib.import_module(name)
    except ImportError:
        module = types.ModuleType(name)
        module.Credentials = object
        module.build = lambda *a, **k: None
        module.service_account = module
        sys.modules[name] = module

import config  # noqa: E402
import main  # noqa: E402
import pending  # noqa: E402
import sheets  # noqa: E402
import state  # noqa: E402
from sources import startups  # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """State files in a temp dir; pending queue under it too."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path / "state" / "pending")
    # A non-empty seen-state, so runs are not treated as the first run.
    (tmp_path / state.STATE_FILE).write_text(json.dumps({"seed": datetime.utcnow().isoformat()}))
    main.SOURCE_OK.clear()
    return tmp_path


# ------------------------------------------------------------------ sharding

def test_every_board_lands_in_exactly_one_shard():
    boards = [("greenhouse", f"co{i}") for i in range(3000)] + [("workday", "tenant:Site")]
    for shards in (3, 6):
        assignment = [startups.shard_of(a, s, shards) for a, s in boards]
        assert set(assignment) == set(range(shards))
        # Deterministic, and case/space-insensitive on the board id.
        assert assignment == [startups.shard_of(a.upper(), f" {s} ", shards) for a, s in boards]
        sizes = [assignment.count(i) for i in range(shards)]
        assert max(sizes) - min(sizes) < len(boards) * 0.05


def test_every_board_fetched_once_a_day_when_all_vendors_are_daily(monkeypatch):
    boards = [{"company": f"C{i}", "ats": "greenhouse", "slug": f"c{i}", "board_url": "u"} for i in range(60)]
    monkeypatch.setattr(startups, "load_boards", lambda: list(boards))
    fetched = []
    monkeypatch.setattr(startups, "fetch_one_board",
                        lambda company, ats, slug, url, source: fetched.append(slug) or [])
    monkeypatch.setattr(startups, "BOARD_DELAY", 0)
    for slot in range(6):  # two days
        result = startups.fetch_all(budget_secs=30, workers=4, slot=slot, runs_per_day=3, max_boards=1000)
        assert result.complete
    # Each board exactly once per day, so twice over the two-day cycle.
    assert sorted(fetched) == sorted(b["slug"] for b in boards for _ in range(2))


def _vendor_boards(counts):
    return [{"company": f"{ats}{i}", "ats": ats, "slug": f"{ats}-{i}", "board_url": "u"}
            for ats, n in counts.items() for i in range(n)]


ORDER = ("personio", "bamboohr", "rippling", "recruitee", "teamtailor", "ashby",
         "breezy", "lever", "workable", "workday", "oracle_hcm", "smartrecruiters", "greenhouse")


def test_cadence_keeps_every_vendor_daily_while_it_fits():
    boards = _vendor_boards({"greenhouse": 300, "ashby": 300, "bamboohr": 60, "personio": 6})
    two_day, largest = startups.plan_cadence(boards, runs_per_day=3, max_boards=600, order=ORDER)
    assert two_day == []
    assert largest <= 600


def test_cadence_overflow_moves_vendors_in_config_order_until_it_fits():
    boards = _vendor_boards({"greenhouse": 900, "ashby": 900, "lever": 300, "bamboohr": 300,
                             "rippling": 300, "personio": 30, "workday": 200})
    two_day, largest = startups.plan_cadence(boards, runs_per_day=3, max_boards=950, order=ORDER)
    # Daily, the largest run is ~977 boards: over 950. Vendors move in config
    # order and stop as soon as it fits; vendors with no boards are skipped.
    assert two_day == ["personio", "bamboohr", "rippling"]
    assert largest <= 950
    # One vendor fewer would not have fit.
    assert startups.plan_cadence(boards, runs_per_day=3, max_boards=950, order=ORDER[:2])[1] > 950
    # A tighter cap moves more, still in order, and never skips ahead.
    tighter, _ = startups.plan_cadence(boards, runs_per_day=3, max_boards=800, order=ORDER)
    assert tighter == ["personio", "bamboohr", "rippling", "ashby"]


def test_cadence_that_cannot_fit_moves_everything_and_reports_the_overflow():
    boards = _vendor_boards({"greenhouse": 3000})
    two_day, largest = startups.plan_cadence(boards, runs_per_day=3, max_boards=100, order=ORDER)
    assert two_day == ["greenhouse"]
    assert largest > 100


def test_two_day_boards_fetched_once_per_cycle_daily_boards_twice():
    boards = _vendor_boards({"personio": 40, "greenhouse": 40})
    two_day = {"personio"}
    for board in boards:
        slots = [s for s in range(6) if startups.in_slot(board, s, 3, two_day)]
        assert len(slots) == (1 if board["ats"] == "personio" else 2)
        if board["ats"] == "greenhouse":
            assert slots[1] - slots[0] == 3  # once each day


def test_run_log_names_two_day_vendors(monkeypatch, capsys):
    boards = _vendor_boards({"greenhouse": 400, "personio": 400})
    monkeypatch.setattr(startups, "load_boards", lambda: list(boards))
    monkeypatch.setattr(startups, "fetch_one_board", lambda *a, **k: [])
    monkeypatch.setattr(startups, "BOARD_DELAY", 0)
    monkeypatch.setattr(startups, "TWO_DAY_VENDOR_ORDER", ORDER)
    startups.fetch_all(budget_secs=30, workers=4, slot=0, runs_per_day=3, max_boards=250)
    out = capsys.readouterr().out
    assert "cadence: two-day vendors: personio" in out
    startups.fetch_all(budget_secs=30, workers=4, slot=0, runs_per_day=3, max_boards=10000)
    assert "cadence: all vendors daily" in capsys.readouterr().out


def test_budget_exhausted_shard_is_incomplete(monkeypatch):
    boards = [{"company": "C", "ats": "greenhouse", "slug": f"c{i}", "board_url": "u"} for i in range(5)]
    monkeypatch.setattr(startups, "load_boards", lambda: list(boards))
    result = startups.fetch_all(budget_secs=0, workers=1)
    assert not result.complete
    assert list(result) == []


def test_oldest_shard_first_and_a_failed_shard_is_retried(workdir):
    shard_state = state.load_shard_state(3)
    order = []
    now = datetime(2026, 9, 17, 4, 0)
    for slot in range(5):
        shard = state.next_shard(shard_state)
        order.append(shard)
        if slot != 1:  # the second slot's run fails: no success recorded
            state.record_shard_success(shard_state, shard, now + timedelta(hours=8 * slot))
        state.save_shard_state(shard_state)
        shard_state = state.load_shard_state(3)
    assert order == [0, 1, 1, 2, 0]


def test_changing_shard_count_resets_shard_history_only(workdir):
    shard_state = state.load_shard_state(3)
    state.record_shard_success(shard_state, 0)
    state.record_daily_source_success(shard_state, "adzuna")
    state.save_shard_state(shard_state)
    reloaded = state.load_shard_state(6)
    assert reloaded["last_success"] == {}
    assert "adzuna" in reloaded["daily_sources"]


def test_daily_sources_anchor_to_the_0417_slot(workdir):
    shard_state = state.load_shard_state(3)
    slot = (4, 17)
    day = datetime(2026, 9, 17)
    assert state.daily_source_due(shard_state, "Adzuna", slot, now=day.replace(hour=9, minute=45))
    # The 04:17 run actually starts at 09:45 (scheduler lag) and succeeds.
    state.record_daily_source_success(shard_state, "Adzuna", day.replace(hour=9, minute=55))
    # The 12:17 and 20:17 runs that day, and the 20:17 run landing after midnight, skip them.
    for when in (day.replace(hour=17, minute=45), day.replace(hour=23, minute=50),
                 day + timedelta(hours=26)):
        assert not state.daily_source_due(shard_state, "Adzuna", slot, now=when)
    # The next day's 04:17 run takes them again, whenever it starts.
    assert state.daily_source_due(shard_state, "Adzuna", slot, now=day + timedelta(days=1, hours=4, minutes=20))


def test_failed_0417_run_leaves_daily_sources_due_for_the_next_slot(workdir):
    shard_state = state.load_shard_state(3)
    yesterday_success = datetime(2026, 9, 16, 9, 50)
    state.record_daily_source_success(shard_state, "JSearch", yesterday_success)
    # Today's 04:17 run failed before recording success; the 12:17 run starts at 17:40.
    assert state.daily_source_due(shard_state, "JSearch", (4, 17), now=datetime(2026, 9, 17, 17, 40))


# ------------------------------------------------------------- pipeline runs

def _job(url, title="Director of IT", company="Acme", source="Startup", score=8):
    return {"title": title, "company": company, "apply_link": url, "source": source,
            "location": "Remote, US", "work_model": "remote", "description": "d", "score": score}


@pytest.fixture
def pipeline(workdir, monkeypatch):
    """main.py with every network edge replaced; records what was fetched, written and sent."""
    calls = {"fetched_sources": [], "shards": [], "sheet": [], "disq": [], "emails": []}
    catalog = {"Startup": [], "adzuna": [], "jsearch": []}

    def source(name):
        def fetch():
            calls["fetched_sources"].append(name)
            return [dict(j) for j in catalog.get(name, [])]
        return fetch

    monkeypatch.setattr(main, "SOURCES", [(n, source(n)) for n, _ in main.SOURCES])

    def fake_startups(slot=None):
        calls["fetched_sources"].append("Startup")
        calls["shards"].append(slot)
        return startups.FetchResult([dict(j) for j in catalog["Startup"]], complete=calls.get("complete", True))

    monkeypatch.setattr(main, "fetch_startups", fake_startups)
    monkeypatch.setattr(main, "load_connections", lambda: {})
    monkeypatch.setattr(main, "find_warm_contact", lambda company, connections: "")
    monkeypatch.setattr(main, "resolve_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_declined_feedback", lambda: [])
    monkeypatch.setattr(main, "score_jobs", lambda jobs, declined_feedback=None: [
        {**j, "rationale": "r", "score": j.get("score", 8)} for j in jobs])
    monkeypatch.setattr(main, "append_jobs", lambda jobs: calls["sheet"].extend(jobs))
    monkeypatch.setattr(main, "append_disqualified_jobs", lambda jobs: calls["disq"].extend(jobs))
    monkeypatch.setattr(main, "send_digest", lambda sections, **kw: calls["emails"].append(sections))
    monkeypatch.setattr(main, "get_unseen_jobs", lambda: [])
    monkeypatch.setitem(sys.modules, "sources.applied_doc", types.SimpleNamespace(seed_from_doc=lambda: None))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: None))
    return calls, catalog


def _emailed_urls(sections):
    return sorted(j["apply_link"] for key in ("main", "worth_a_look", "cool_wrong_role")
                  for j in sections.get(key, []))


def test_posting_found_in_any_shard_is_emailed_once_and_never_again(pipeline):
    calls, catalog = pipeline
    catalog["Startup"] = [_job("https://boards.greenhouse.io/acme/jobs/1")]
    main.fetch_run()                         # slot 1 finds it
    catalog["Startup"] = [_job("https://boards.greenhouse.io/acme/jobs/1"),
                          _job("https://jobs.lever.co/beta/2", company="Beta")]
    main.fetch_run()                         # slot 2 sees it again, plus a new one
    main.digest_run()
    assert len(calls["emails"]) == 1
    assert _emailed_urls(calls["emails"][0]) == ["https://boards.greenhouse.io/acme/jobs/1",
                                                 "https://jobs.lever.co/beta/2"]
    main.fetch_run()                         # slot 3 sees both again
    main.digest_run()
    assert len(calls["emails"]) == 1         # nothing new: no second email
    assert calls["shards"] == [0, 1, 2]


def test_same_posting_from_two_runs_under_different_urls_is_sent_once(pipeline):
    calls, catalog = pipeline
    catalog["Startup"] = [_job("https://boards.greenhouse.io/acme/jobs/1")]
    main.fetch_run()
    catalog["Startup"] = []
    # Later the same day JSearch reports it under an aggregator URL.
    catalog["jsearch"] = [_job("https://jsearch.example/abc", source="JSearch")]
    main.fetch_run(force_daily=True)
    main.digest_run()
    assert _emailed_urls(calls["emails"][0]) == ["https://boards.greenhouse.io/acme/jobs/1"]


def test_daily_sources_run_on_one_fetch_run_a_day(pipeline):
    calls, _ = pipeline
    for _ in range(3):
        main.fetch_run()  # three runs inside one day: only the first takes the daily sources
    for name in config.DAILY_SOURCES:
        assert calls["fetched_sources"].count(name) == 1, name
    assert calls["fetched_sources"].count("Startup") == 3


def test_manual_dispatch_forces_the_daily_sources(pipeline):
    calls, _ = pipeline
    main.fetch_run()
    main.fetch_run(force_daily=True)
    assert calls["fetched_sources"].count("adzuna") == 2


def test_incomplete_shard_is_taken_again(pipeline):
    calls, _ = pipeline
    calls["complete"] = False
    main.fetch_run()
    calls["complete"] = True
    main.fetch_run()
    main.fetch_run()
    assert calls["shards"] == [0, 0, 1]


def test_batch_queued_during_a_digest_waits_for_the_next_digest(pipeline, monkeypatch):
    calls, catalog = pipeline
    catalog["Startup"] = [_job("https://a.example/1")]
    main.fetch_run()

    real_send = main.send_digest

    def send_while_a_fetch_lands(sections, **kw):
        # A fetch run commits a new batch while this digest is sending.
        pending.write_batch([{**_job("https://b.example/2", company="B"), "found_at": "2026-09-17T12:00:00"}],
                            "shard1of3")
        real_send(sections, **kw)

    monkeypatch.setattr(main, "send_digest", send_while_a_fetch_lands)
    main.digest_run()
    assert _emailed_urls(calls["emails"][0]) == ["https://a.example/1"]
    monkeypatch.setattr(main, "send_digest", real_send)
    main.digest_run()
    assert _emailed_urls(calls["emails"][1]) == ["https://b.example/2"]


def test_failed_email_keeps_the_queue(pipeline, monkeypatch):
    calls, catalog = pipeline
    catalog["Startup"] = [_job("https://a.example/1")]
    main.fetch_run()

    def boom(sections, **kw):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(main, "send_digest", boom)
    with pytest.raises(RuntimeError):
        main.digest_run()
    assert pending.load_pending()[1], "queue must survive a failed send"


def test_empty_digest_sends_nothing_and_counts_a_skipped_day(pipeline):
    calls, _ = pipeline
    main.fetch_run()
    main.digest_run()
    assert calls["emails"] == []
    assert state.get_skip_stats() == (1, 1)


def test_score_zero_goes_to_disqualified_with_fetch_time(pipeline):
    calls, catalog = pipeline
    catalog["Startup"] = [_job("https://a.example/1", score=0)]
    main.fetch_run()
    main.digest_run()
    assert [j["apply_link"] for j in calls["disq"]] == ["https://a.example/1"]
    assert calls["disq"][0]["found_at"]


def test_sheet_date_first_seen_is_the_fetch_date():
    assert sheets._first_seen({"found_at": "2026-09-15T22:17:03"}) == "2026-09-15"
    assert sheets._first_seen({}) == datetime.utcnow().strftime("%Y-%m-%d")


def test_pending_write_is_atomic(workdir):
    path = pending.write_batch([_job("https://a.example/1")], "shard0of3")
    assert path.exists()
    assert not list(path.parent.glob("*.tmp-*"))
    assert json.loads(path.read_text())["jobs"][0]["apply_link"] == "https://a.example/1"


# ------------------------------------------------ Bespoke commit after queue

def test_bespoke_state_is_committed_only_after_its_postings_are_queued(pipeline, monkeypatch):
    calls, _ = pipeline
    seen_at_commit = []

    class Jobs(list):
        def commit(self):
            seen_at_commit.append([j["apply_link"] for j in pending.load_pending()[1]])

    def bespoke_fetch():
        return Jobs([_job("https://careers.example.org/jobs/it-director", company="Example Org",
                          source="Bespoke")])

    names = [n for n, _ in main.SOURCES]
    sources = dict(main.SOURCES)
    sources["bespoke"] = bespoke_fetch
    monkeypatch.setattr(main, "SOURCES", [(n, sources[n]) for n in names])
    main.fetch_run()
    assert seen_at_commit == [["https://careers.example.org/jobs/it-director"]]


def test_abandoned_bespoke_is_cancelled_and_never_commits(pipeline, monkeypatch):
    import threading
    import time as _time
    calls, _ = pipeline
    committed, cancelled = [], threading.Event()

    class Jobs(list):
        def commit(self):
            committed.append(True)

    def slow_bespoke():
        _time.sleep(1.0)
        return Jobs()

    slow_bespoke.cancel = cancelled.set
    names = [n for n, _ in main.SOURCES]
    sources = dict(main.SOURCES)
    sources["bespoke"] = slow_bespoke
    monkeypatch.setattr(main, "SOURCES", [(n, sources[n]) for n in names])
    monkeypatch.setitem(config.SOURCE_BUDGET_SECS, "bespoke", 0.1)
    monkeypatch.setitem(main.SOURCE_BUDGET_SECS, "bespoke", 0.1)
    main.fetch_run()
    assert cancelled.is_set()
    assert committed == []
