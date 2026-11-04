"""PUB-08: DST-proof schedule from the profile.

Covers: schedule.py's lines per UTC offset (New York two each, Phoenix one,
Sydney two with the opposite seasons, a lead crossing midnight moving the
day-of-week field); the slot guard (on-slot run proceeds, its DST twin exits 0
before any file or network access, including on the two switch mornings and
hours into the queue; workflow_dispatch ignores it) for main.py digest and
weekly_summary.py; `cron --write` idempotent and touching only the schedule
block; delivery.weekly_summary_time_local validation; the applied_doc model.
No network access.
"""

import builtins
import io
import os
import runpy
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import profile_loader  # noqa: E402
import schedule  # noqa: E402

LEAD = config.CRON_LEAD_MINUTES
WINDOW = config.SLOT_GUARD_MINUTES
COMMITTED = profile_loader.active()["delivery"]
NY = dict(COMMITTED, timezone="America/New_York", digest_time_local="08:00", weekly_summary_time_local="08:00")


def _lines(delivery, start=date(2026, 10, 28)):
    return {k: [l.cron for l in v] for k, v in schedule.lines(delivery, LEAD, start=start).items()}


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- Lines per offset ------------------------------------------------------------


def test_new_york_has_two_lines_each():
    assert schedule.offsets(schedule.ZoneInfo("America/New_York"), date(2026, 10, 28)) == \
        [timedelta(hours=-5), timedelta(hours=-4)]
    # Digest 02:17 local: 07:17 UTC in EST, 06:17 UTC in EDT. Weekly 08:00 local
    # lands on the hour (13:00 / 12:00 UTC), so it fires 3 min earlier.
    assert _lines(NY) == {"digest": ["17 7 * * 0-5", "17 6 * * 0-5"],
                          "weekly": ["57 12 * * 6", "57 11 * * 6"]}


def test_phoenix_has_one_line_each():
    assert _lines(dict(NY, timezone="America/Phoenix")) == {"digest": ["17 9 * * 0-5"],
                                                            "weekly": ["57 14 * * 6"]}


def test_sydney_has_two_lines_with_the_opposite_seasons():
    tz = schedule.ZoneInfo("Australia/Sydney")
    assert schedule.offsets(tz, date(2026, 10, 28)) == [timedelta(hours=10), timedelta(hours=11)]
    # 02:17 local Sun-Fri is the previous UTC day: 16:17 (AEST) / 15:17 (AEDT) Sat-Thu.
    assert _lines(dict(NY, timezone="Australia/Sydney")) == {
        "digest": ["17 16 * * 0-4,6", "17 15 * * 0-4,6"],
        "weekly": ["57 21 * * 5", "57 20 * * 5"]}
    # In late October Sydney is on daylight time (+11) while New York still is too.
    assert _utc(2026, 10, 28, 12).astimezone(tz).utcoffset() == timedelta(hours=11)
    assert _utc(2026, 7, 1, 12).astimezone(tz).utcoffset() == timedelta(hours=10)


def test_a_lead_crossing_midnight_shifts_the_day_of_week_field():
    d = dict(NY, digest_time_local="03:00", digest_days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    # 03:00 - 5 h 43 min = 21:17 local the evening before (Sun-Thu); +4/+5 h is
    # 01:17 / 02:17 UTC, back on the digest day itself (Mon-Fri).
    assert schedule.slots(d, LEAD)["digest"] == schedule.Slot(21 * 60 + 17, ["Sunday", "Monday", "Tuesday",
                                                                             "Wednesday", "Thursday"])
    assert _lines(d)["digest"] == ["17 2 * * 1-5", "17 1 * * 1-5"]


def test_weekly_time_defaults_to_0800():
    d = {k: v for k, v in NY.items() if k != "weekly_summary_time_local"}
    assert schedule.slots(d, LEAD)["weekly"].minutes == 8 * 60


def test_the_block_carries_the_generated_comment():
    text = schedule.block("digest", NY, LEAD, start=date(2026, 10, 28))
    assert text.splitlines()[:2] == [f"  {schedule.GENERATED_COMMENT}", "  schedule:"]
    assert "    - cron: '17 6 * * 0-5'   # 02:17 America/New_York when it is UTC-04:00" in text


# --- cron --write ----------------------------------------------------------------


def _copies(tmp_path):
    out = {}
    for name, path in schedule.WORKFLOWS.items():
        out[name] = tmp_path / path.name
        out[name].write_text(path.read_text())
    return out


def _outside(text):
    start, end = schedule._span(text)
    return text[:start], text[end:]


def test_cron_write_is_idempotent_and_touches_only_the_schedule_block(tmp_path):
    files = _copies(tmp_path)
    before = {n: p.read_text() for n, p in files.items()}
    # A profile in another zone changes both blocks...
    denver = dict(COMMITTED, timezone="America/Denver")
    assert schedule.write(denver, LEAD, workflows=files) == ["digest", "weekly"]
    after = {n: p.read_text() for n, p in files.items()}
    for name in files:
        assert after[name] != before[name]
        assert _outside(after[name]) == _outside(before[name])   # the rest byte for byte
        assert "'17 8 * * 0-5'" in after[name] or "'17 15 * * 6'" in after[name]
    # ...a second run changes nothing...
    assert schedule.write(denver, LEAD, workflows=files) == []
    assert {n: p.read_text() for n, p in files.items()} == after
    # ...and writing the committed profile back restores the files exactly.
    assert schedule.write(COMMITTED, LEAD, workflows=files) == ["digest", "weekly"]
    assert {n: p.read_text() for n, p in files.items()} == before


def test_a_hand_edit_is_reported_and_replaced(tmp_path):
    files = _copies(tmp_path)
    text = files["digest"].read_text()
    files["digest"].write_text(text.replace("'17 7 * * 0-5'", "'17 9 * * 0-5'"))
    assert list(schedule.mismatches(COMMITTED, LEAD, workflows=files)) == ["digest"]
    schedule.write(COMMITTED, LEAD, workflows=files)
    assert files["digest"].read_text() == text


def test_the_workflows_guard_only_a_schedule_trigger():
    # PUB-06 moved the guard into each workflow's first job (`slot`), which
    # checks only schedule: runs; tests/test_pub06_setup_commands.py covers it.
    for path in schedule.WORKFLOWS.values():
        text = path.read_text()
        assert 'if [ "${{ github.event_name }}" != "schedule" ]; then' in text
        assert "SCHEDULED_CRON: ${{ github.event.schedule }}" in text
    assert "SCHEDULED_CRON" not in (ROOT / ".github" / "workflows" / "fetch.yml").read_text()


# --- Slot guard ------------------------------------------------------------------


def _on(name, delivery, cron, now):
    return schedule.on_slot(name, delivery, LEAD, WINDOW, now, cron)[0]


@pytest.mark.parametrize("now", [_utc(2026, 10, 28, 6, 20), _utc(2026, 10, 28, 11, 40)])
def test_daylight_time_runs_the_daylight_line_however_late_it_starts(now):
    # 11:40 UTC: 5 h 23 min of queue delay, still the 06:17 line's run.
    assert _on("digest", NY, "17 6 * * 0-5", now)
    assert not _on("digest", NY, "17 7 * * 0-5", now + timedelta(hours=1))


def test_standard_time_runs_the_standard_line():
    assert _on("digest", NY, "17 7 * * 0-5", _utc(2026, 12, 2, 9, 0))
    assert not _on("digest", NY, "17 6 * * 0-5", _utc(2026, 12, 2, 9, 0))


def test_the_switch_mornings_run_exactly_one_line():
    # Sunday 2026-03-08, clocks jump at 07:00 UTC: delivery 08:00 EDT = 12:00
    # UTC, so the run 343 min earlier is the 06:17 line (01:17 EST on the clock).
    assert _on("digest", NY, "17 6 * * 0-5", _utc(2026, 3, 8, 8, 0))
    assert not _on("digest", NY, "17 7 * * 0-5", _utc(2026, 3, 8, 8, 0))
    # Sunday 2026-11-01, clocks fall back at 06:00 UTC: the 07:17 line.
    assert _on("digest", NY, "17 7 * * 0-5", _utc(2026, 11, 1, 8, 0))
    assert not _on("digest", NY, "17 6 * * 0-5", _utc(2026, 11, 1, 8, 0))


def test_weekly_and_sydney_twins():
    assert _on("weekly", NY, "57 11 * * 6", _utc(2026, 10, 31, 13, 0))
    assert not _on("weekly", NY, "57 12 * * 6", _utc(2026, 10, 31, 13, 0))
    syd = dict(NY, timezone="Australia/Sydney")
    assert _on("digest", syd, "17 15 * * 0-4,6", _utc(2026, 10, 28, 16, 0))       # AEDT
    assert not _on("digest", syd, "17 16 * * 0-4,6", _utc(2026, 10, 28, 17, 0))


def test_without_the_cron_line_the_start_time_is_compared():
    assert _on("digest", NY, None, _utc(2026, 10, 28, 6, 40))
    assert not _on("digest", NY, None, _utc(2026, 10, 28, 7, 17))


class _Frozen(datetime):
    at = None

    @classmethod
    def now(cls, tz=None):
        return cls.at.astimezone(tz) if tz else cls.at


@pytest.fixture
def frozen(monkeypatch):
    def freeze(at):
        _Frozen.at = at
        monkeypatch.setattr(schedule, "datetime", _Frozen)
    return freeze


def _no_io(monkeypatch):
    """Any file open or socket connect from here on fails the test."""
    def boom(*a, **k):
        raise AssertionError(f"file or network access after the guard: {a[:1]}")
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(io, "open", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


def _run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    # The first pipeline import after the guard: None makes `import pending`
    # raise ImportError, which is how a run that got past the guard shows.
    monkeypatch.setitem(sys.modules, "pending", None)
    runpy.run_path(str(ROOT / "main.py"), run_name="__main__")


def test_main_digest_off_slot_twin_exits_0_before_any_io(monkeypatch, frozen, capsys):
    frozen(_utc(2026, 10, 28, 9, 30))
    monkeypatch.setenv("SCHEDULED_CRON", "17 8 * * 0-5")   # CST line, in daylight time
    _no_io(monkeypatch)
    with pytest.raises(SystemExit) as exit_:
        _run_main(monkeypatch, "digest", "--scheduled")
    assert exit_.value.code == 0
    assert "[schedule] off-slot run for America/Chicago (DST twin), exiting" in capsys.readouterr().out


def test_main_digest_on_slot_run_proceeds(monkeypatch, frozen, capsys):
    frozen(_utc(2026, 10, 28, 9, 30))
    monkeypatch.setenv("SCHEDULED_CRON", "17 7 * * 0-5")
    with pytest.raises(ImportError):
        _run_main(monkeypatch, "digest", "--scheduled")
    assert "[schedule] on-slot run" in capsys.readouterr().out


def test_main_digest_dispatch_ignores_the_guard(monkeypatch, frozen, capsys):
    frozen(_utc(2026, 10, 28, 9, 30))
    monkeypatch.setenv("SCHEDULED_CRON", "17 7 * * 0-5")
    with pytest.raises(ImportError):
        _run_main(monkeypatch, "digest")
    assert "[schedule]" not in capsys.readouterr().out


class _Proceeded(Exception):
    pass


def _weekly(monkeypatch):
    import weekly_summary

    def proceeded():
        raise _Proceeded()
    monkeypatch.setattr(weekly_summary, "_get_sheet", proceeded)
    monkeypatch.setattr(weekly_summary.state, "reset_skip_stats", proceeded)
    return weekly_summary


def test_weekly_off_slot_twin_returns_before_the_sheet(monkeypatch, frozen, capsys):
    ws = _weekly(monkeypatch)
    frozen(_utc(2026, 10, 31, 14, 0))
    monkeypatch.setenv("SCHEDULED_CRON", "17 14 * * 6")    # CST line, in daylight time
    _no_io(monkeypatch)
    ws.main(["--scheduled"])                                # returns: exit 0
    assert "off-slot run for America/Chicago (DST twin), exiting" in capsys.readouterr().out


def test_weekly_on_slot_and_dispatch_proceed(monkeypatch, frozen):
    ws = _weekly(monkeypatch)
    frozen(_utc(2026, 10, 31, 14, 0))
    monkeypatch.setenv("SCHEDULED_CRON", "17 13 * * 6")
    with pytest.raises(_Proceeded):
        ws.main(["--scheduled"])
    monkeypatch.setenv("SCHEDULED_CRON", "17 14 * * 6")
    with pytest.raises(_Proceeded):
        ws.main([])


# --- Profile and reversals -------------------------------------------------------


@pytest.mark.parametrize("value", ["8:00", "24:00", "08:60", "noon"])
def test_weekly_summary_time_must_be_hh_mm(value):
    import copy
    data = copy.deepcopy(profile_loader.read())
    data["delivery"]["weekly_summary_time_local"] = value
    assert any("delivery.weekly_summary_time_local" in e for e in profile_loader.validate(data))


def test_weekly_summary_time_may_be_left_out():
    import copy
    data = copy.deepcopy(profile_loader.read())
    del data["delivery"]["weekly_summary_time_local"]
    assert profile_loader.validate(data) == []


def test_applied_doc_uses_the_scoring_model():
    from sources import applied_doc
    assert applied_doc.MODEL == profile_loader.active()["scoring"]["model"]
    assert "model=PROFILE_MODEL" not in (ROOT / "sources" / "applied_doc.py").read_text()
