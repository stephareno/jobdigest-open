"""GAP-08: the weekly summary runs on Saturday, the no-digest day.

Two things have to hold together. The workflow must fire on Saturday off the top
of the hour, and the eight-day window must land on the same cutoff date the old
Friday run used — moving the run a day later must not move the week it reports.
No network access.
"""

import os
import sys
import types
from datetime import date, timedelta

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule  # noqa: E402

SATURDAY = 5  # date.weekday(): Monday is 0
FRIDAY = 4


def _load_weekly_summary():
    """Import weekly_summary without gspread, Google credentials or real secrets.

    The module opens a Sheet client and an Anthropic client at import time, so
    the test stubs both dependencies and the env vars they read.
    """
    for key, value in {
        "GMAIL_ADDRESS": "test@example.com",
        "GMAIL_APP_PASSWORD": "x",
        "ANTHROPIC_API_KEY": "x",
        "GOOGLE_SHEET_ID": "x",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
    }.items():
        os.environ.setdefault(key, value)

    if "gspread" not in sys.modules:
        gspread = types.ModuleType("gspread")
        gspread.authorize = lambda creds: None
        sys.modules["gspread"] = gspread

    if "google.oauth2.service_account" not in sys.modules:
        google = sys.modules.setdefault("google", types.ModuleType("google"))
        oauth2 = sys.modules.setdefault("google.oauth2", types.ModuleType("google.oauth2"))
        service_account = types.ModuleType("google.oauth2.service_account")

        class _Credentials:
            @staticmethod
            def from_service_account_info(info, scopes=None):
                return None

        service_account.Credentials = _Credentials
        sys.modules["google.oauth2.service_account"] = service_account
        google.oauth2 = oauth2
        oauth2.service_account = service_account

    import weekly_summary
    return weekly_summary


# --- workflow schedule -------------------------------------------------------

def _workflow():
    with open(schedule.WORKFLOWS["weekly"]) as f:
        # "on:" is a YAML 1.1 boolean, so the parsed key is True, not "on".
        return yaml.safe_load(f)


def test_cron_fires_saturday_off_the_top_of_the_hour():
    crons = [s["cron"] for s in _workflow()[True]["schedule"]]
    # PUB-08: one line per UTC offset (CST, CDT); the slot guard picks.
    assert crons == ["17 14 * * 6", "17 13 * * 6"], crons
    for cron in crons:
        minute, hour, dom, month, dow = cron.split()
        assert dow == "6", "Saturday is the no-digest day (GAP-08)"
        assert minute != "0", "stay off the top of the hour, like daily.yml and fetch.yml"


def test_still_shares_the_repo_state_writers_concurrency_group():
    # PUB-06: job level, so the slot-guard job stays outside the group.
    concurrency = _workflow()["jobs"]["weekly-summary"]["concurrency"]
    assert concurrency["group"] == "repo-state-writers"
    assert concurrency["cancel-in-progress"] is False


def test_workflow_still_resets_the_skipped_day_counter():
    with open(schedule.WORKFLOWS["weekly"]) as f:
        text = f.read()
    assert "skip_state.json" in text
    assert "Reset weekly skipped-day counter" in text


def test_summary_still_resets_the_counter_after_sending():
    import inspect
    weekly_summary = _load_weekly_summary()
    source = inspect.getsource(weekly_summary.main)
    assert source.index("_send_summary") < source.index("state.reset_skip_stats"), \
        "the counter is reset only after the email goes out"


# --- window alignment --------------------------------------------------------

def _cutoff(run_day, days):
    return run_day - timedelta(days=days)


def test_saturday_window_starts_on_the_same_day_the_friday_run_used():
    weekly_summary = _load_weekly_summary()
    # Four consecutive weeks, to catch an off-by-one that only shows at a month
    # or DST boundary.
    for friday in [date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 30),
                   date(2026, 11, 6)]:
        assert friday.weekday() == FRIDAY
        saturday = friday + timedelta(days=1)
        assert saturday.weekday() == SATURDAY
        old = _cutoff(friday, 7)           # Friday 12:00 UTC run, seven days back
        new = _cutoff(saturday, weekly_summary.LOOKBACK_DAYS)
        assert new == old, f"window shifted: {new} vs {old}"
        assert new.weekday() == FRIDAY


def test_window_covers_the_whole_sunday_to_friday_week_it_follows():
    weekly_summary = _load_weekly_summary()
    saturday = date(2026, 9, 26)
    cutoff = _cutoff(saturday, weekly_summary.LOOKBACK_DAYS)
    # The digest week that just ended: Sunday 20 Sep through Friday 25 Sep.
    week = [date(2026, 9, 20) + timedelta(days=i) for i in range(6)]
    assert week[0].weekday() == 6 and week[-1].weekday() == FRIDAY
    for day in week:
        assert day >= cutoff, f"{day} fell outside the window"


def test_friday_evening_fetches_are_counted_exactly_once():
    """Rows dated Friday from the 12:17 and 20:17 fetches only reach the Sheet in
    Sunday's digest, so they must be picked up by the following Saturday's run
    and not by the one before it."""
    weekly_summary = _load_weekly_summary()
    days = weekly_summary.LOOKBACK_DAYS
    friday = date(2026, 9, 25)
    same_week_saturday = friday + timedelta(days=1)   # too early: not in the Sheet yet
    next_saturday = friday + timedelta(days=8)
    week_after = friday + timedelta(days=15)

    assert friday >= _cutoff(same_week_saturday, days)  # in range, but unwritten
    assert friday >= _cutoff(next_saturday, days), "the run that first sees the row"
    assert friday < _cutoff(week_after, days), "and only that run"


def test_seven_day_window_from_saturday_would_have_dropped_those_rows():
    """Why LOOKBACK_DAYS is 8 and not 7 — the regression this guards against."""
    friday = date(2026, 9, 25)
    next_saturday = friday + timedelta(days=8)
    assert friday < _cutoff(next_saturday, 7)
