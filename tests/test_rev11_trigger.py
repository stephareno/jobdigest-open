"""REV-11: on-time runs through an external trigger (workflow_dispatch, scheduled=true).

Covers: a dispatched fetch with --by-clock takes the startup slot for its time
of day and the daily sources only when due; a run a little before the daily
slot counts as that slot's run; the run kind reaches the skip counter and the
alert-only email (trigger = the scheduled run, manual = neither); the
workflows carry the input and pass it on; doctor's lag computation; the
trigger-setup text. No network access.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from tests.test_rev02_failure_alert import (  # noqa: F401  (fixtures)
    _failed, _runs_file, _skip_state, digest, pipeline)

import config  # noqa: E402
import doctor  # noqa: E402
import main  # noqa: E402
import profile_loader  # noqa: E402
import state  # noqa: E402
import trigger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SLOTS = config.FETCH_SLOTS_UTC


def _shard_state(last=None, daily=None):
    return {"shards": config.FETCH_CYCLE_SLOTS, "last_success": dict(last or {}),
            "daily_sources": dict(daily or {})}


def _at(text):
    return datetime.fromisoformat(text)


# --------------------------------------------------------------- fetch: slot by clock

@pytest.mark.parametrize("now, run_of_day", [
    ("2026-11-25T04:17", 0), ("2026-11-25T04:05", 0),   # on time, Apps Script a few minutes early
    ("2026-11-25T03:17", 0),                            # a local-time job in daylight time, an hour early
    ("2026-11-25T12:20", 1), ("2026-11-25T20:17", 2), ("2026-11-25T23:59", 2),
])
def test_the_run_of_day_is_the_nearest_fetch_slot(now, run_of_day):
    assert trigger.clock_run_of_day(_at(now), SLOTS) == run_of_day


def test_clock_shard_takes_the_older_of_the_two_slots_for_that_time_of_day():
    # Slot 0 ran yesterday, slot 3 two days ago: the 04:17 run takes 3, never 1 or 2.
    ss = _shard_state({"0": "2026-11-24T04:20:00", "3": "2026-11-23T04:20:00",
                       "1": "2026-11-20T00:00:00", "2": "2026-11-20T00:00:00"})
    assert trigger.clock_shard(ss, _at("2026-11-25T04:17"), SLOTS) == 3
    assert trigger.clock_shard(ss, _at("2026-11-25T12:17"), SLOTS) == 4    # never fetched: first
    ss["last_success"].update({"4": "2026-11-24T12:20:00", "1": "2026-11-23T12:20:00"})
    assert trigger.clock_shard(ss, _at("2026-11-25T12:17"), SLOTS) == 1


def test_oldest_overall_would_pick_another_slot_by_clock_does_not():
    # The pre-REV-11 rule takes the oldest slot (2) whatever the hour.
    ss = _shard_state({"0": "2026-11-24T04:20", "1": "2026-11-24T12:20", "2": "2026-11-22T20:20",
                       "3": "2026-11-23T04:20", "4": "2026-11-23T12:20", "5": "2026-11-23T20:20"})
    assert state.next_shard(ss) == 2
    shard, _ = main._pick_fetch_plan(ss, "auto", by_clock=True, now=_at("2026-11-25T04:17"))
    assert shard == 3


def test_by_clock_takes_daily_sources_only_when_due(pipeline):
    enabled = main._enabled(config.DAILY_SOURCES)
    assert enabled
    done_today = {name: "2026-11-25T04:20:00" for name in enabled}
    shard, daily = main._pick_fetch_plan(_shard_state(daily=done_today), "auto", by_clock=True,
                                         now=_at("2026-11-25T12:17"))
    assert shard in (1, 4) and daily == []                          # a manual dispatch would force them all
    _, daily = main._pick_fetch_plan(_shard_state(daily=done_today), "auto", force_daily=True,
                                     now=_at("2026-11-25T12:17"))
    assert daily == enabled
    _, daily = main._pick_fetch_plan(_shard_state(daily={n: "2026-11-24T04:20:00" for n in enabled}),
                                     "auto", by_clock=True, now=_at("2026-11-25T04:17"))
    assert daily == enabled


def test_an_early_run_counts_as_the_daily_slots_run():
    slot = config.DAILY_SOURCE_SLOT_UTC
    ss = _shard_state(daily={"adzuna": "2026-11-25T03:17:00"})       # an hour early (daylight time)
    assert not state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-25T11:17"))
    assert not state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-25T19:17"))
    assert state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-26T03:17"))   # next day's early run
    # Cron runs start late: unchanged.
    ss = _shard_state(daily={"adzuna": "2026-11-25T04:49:00"})
    assert not state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-25T14:16"))
    assert state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-26T04:30"))
    # Too early to count: 3 h before the slot is the previous day's window.
    ss = _shard_state(daily={"adzuna": "2026-11-25T01:00:00"})
    assert state.daily_source_due(ss, "adzuna", slot, now=_at("2026-11-25T04:17"))


def test_main_accepts_by_clock_for_fetch_only(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "fetch_run", lambda **kw: calls.append(kw))
    main.main("fetch", by_clock=True)
    assert calls == [{"force_daily": False, "by_clock": True}]


# --------------------------------------------------------------- run kind: skip state and alerts

def test_run_kind_default_and_values():
    assert trigger.run_kind({}) == "schedule"
    assert trigger.run_kind({"RUN_KIND": "trigger"}) == "trigger"
    assert not trigger.counts_as_scheduled({"RUN_KIND": "manual"})
    with pytest.raises(ValueError):
        trigger.run_kind({"RUN_KIND": "cron"})


def test_a_triggered_run_is_the_scheduled_one(digest, pipeline, monkeypatch, capsys):
    monkeypatch.setenv("RUN_KIND", "trigger")
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()
    assert "[Run] scheduled run (external trigger)" in capsys.readouterr().out
    assert [s for s, _ in digest] == ["Job digest: fetch runs failed"]
    skip = _skip_state(pipeline)
    today = profile_loader.local_today().isoformat()
    assert skip["skipped_days"] == [today] and skip["alert_emails"] == [today]
    assert skip["reported_fetch_runs"] == ["104"]


def test_a_manual_run_adds_no_skipped_day_and_leaves_the_alert(digest, pipeline, monkeypatch, capsys):
    monkeypatch.setenv("RUN_KIND", "manual")
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()
    out = capsys.readouterr().out
    assert "[Run] manual run" in out and "the next scheduled digest sends it" in out
    assert digest == []
    skip = state._load_skip_state()
    assert skip["skipped_days"] == [] and skip["run_days"] == []
    assert skip["alert_emails"] == [] and skip["reported_fetch_runs"] == []
    # The next scheduled (triggered) digest sends it.
    monkeypatch.setenv("RUN_KIND", "trigger")
    main.digest_run()
    assert [s for s, _ in digest] == ["Job digest: fetch runs failed"]
    assert _skip_state(pipeline)["reported_fetch_runs"] == ["104"]


def test_a_manual_run_with_nothing_queued_records_nothing(digest, pipeline, monkeypatch):
    monkeypatch.setenv("RUN_KIND", "manual")
    main.digest_run()
    assert digest == [] and state._load_skip_state()["skipped_days"] == []
    monkeypatch.setenv("RUN_KIND", "schedule")
    main.digest_run()
    assert _skip_state(pipeline)["skipped_days"] == [profile_loader.local_today().isoformat()]


# --------------------------------------------------------------- workflows

@pytest.mark.parametrize("name", ["daily.yml", "fetch.yml", "weekly_summary.yml"])
def test_each_workflow_takes_the_scheduled_input(name):
    import yaml
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    assert data[True]["workflow_dispatch"]["inputs"]["scheduled"]["type"] == "boolean"
    assert "(external trigger)" in data["run-name"]
    assert "schedule" in data[True]                                   # the cron lines stay


def test_the_workflows_pass_the_run_kind_on():
    kind = ("RUN_KIND: ${{ github.event_name == 'schedule' && 'schedule' || "
            "((inputs.scheduled == true || inputs.scheduled == 'true') && 'trigger' || 'manual') }}")
    assert kind in (WORKFLOWS / "daily.yml").read_text()
    fetch = (WORKFLOWS / "fetch.yml").read_text()
    assert kind in fetch
    assert ("python main.py fetch ${{ github.event_name == 'workflow_dispatch' && ((inputs.scheduled == true || "
            "inputs.scheduled == 'true') && '--by-clock' || '--daily-sources') || '' }}") in fetch


def test_fetch_slots_match_fetch_yml():
    cron = re.search(r"- cron: '([^']+)'", (WORKFLOWS / "fetch.yml").read_text()).group(1)
    minute, hours = cron.split()[:2]
    assert tuple((int(h), int(minute)) for h in hours.split(",")) == config.FETCH_SLOTS_UTC
    assert config.DAILY_SOURCE_SLOT_UTC == config.FETCH_SLOTS_UTC[0]


# --------------------------------------------------------------- doctor: lag

def _run(path, started, name="Job Fetch (sharded)"):
    return {"name": name, "path": f".github/workflows/{path}", "run_started_at": started,
            "created_at": started}


def test_lag_is_start_minus_the_latest_cron_minute():
    assert trigger.crons_by_path()[".github/workflows/fetch.yml"] == ["17 4,12,20 * * *"]
    # daily.yml's lines depend on the profile (the export regenerates them), so pinned here.
    crons = {".github/workflows/fetch.yml": ["17 4,12,20 * * *"],
             ".github/workflows/daily.yml": ["17 7 * * 0-5", "17 6 * * 0-5"]}
    lags = trigger.run_lags([_run("fetch.yml", "2026-11-25T14:16:46Z"),
                             _run("daily.yml", "2026-11-25T17:50:19Z", "Job Digest Agent"),   # a Wednesday
                             _run("audit_dead_links.yml", "2026-11-25T10:00:00Z")], crons)
    assert [(n, lag) for n, _, lag in lags] == [("Job Fetch (sharded)", 120), ("Job Digest Agent", 633)]


def test_doctor_warns_above_an_hour():
    late = lambda: [_run("fetch.yml", "2026-11-25T14:16:46Z"), _run("fetch.yml", "2026-11-25T04:40:00Z")]
    [r] = doctor.scheduler_lag(late)
    assert r["status"] == doctor.WARN
    assert r["reason"].startswith("GitHub's scheduler is late; see python main.py trigger-setup")
    assert "+120 min" in r["reason"] and "+23 min" in r["reason"]
    [r] = doctor.scheduler_lag(lambda: [_run("fetch.yml", "2026-11-25T04:40:00Z")])
    assert r["status"] == doctor.PASS and "+23 min" in r["reason"]


def test_doctor_skips_when_gh_cannot_answer():
    def broken():
        raise RuntimeError("gh: Not Found (HTTP 404)")
    [r] = doctor.scheduler_lag(broken)
    assert r["status"] == doctor.SKIP


# --------------------------------------------------------------- trigger-setup

def test_trigger_setup_names_every_call(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    delivery = {"timezone": "America/New_York", "digest_time_local": "08:00",
                "digest_days": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                "weekly_summary_day": "Saturday", "weekly_summary_time_local": "08:17"}
    text = trigger.setup_text(delivery, config.CRON_LEAD_MINUTES, SLOTS, trigger.repo_slug())
    for wf in ("daily.yml", "fetch.yml", "weekly_summary.yml"):
        assert f"POST https://api.github.com/repos/owner/repo/actions/workflows/{wf}/dispatches" in text
    assert "digest           02:17 America/New_York, Sunday, Monday" in text
    assert "fetch 04:17 UTC  04:17 UTC (23:17 or 00:17 America/New_York), every day" in text
    assert "weekly summary   08:17 America/New_York, Saturday" in text
    assert "Authorization: Bearer <YOUR_GITHUB_TOKEN>" in text
    assert '{"ref": "main", "inputs": {"scheduled": "true"}}' in text
    assert "Actions: Read and write. Nothing else." in text and "one year" in text
    assert "Only select repositories > owner/repo" in text
    assert "cron-job.org" in text and "PropertiesService.getScriptProperties()" in text
    script = text[text.index("// Job digest"):text.index("5. Check it")]
    assert len([l for l in script.splitlines() if l.strip()]) <= 22


def test_repo_slug_reads_the_remote(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    fake = lambda *a, **k: type("R", (), {"stdout": "git@github.com:someone/thing.git\n"})()
    assert trigger.repo_slug(run=fake) == "someone/thing"
