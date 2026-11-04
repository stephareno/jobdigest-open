"""PUB-05: sources and delivery from profile.yaml; the cron helper.

Covers: Adzuna locations and Built In URLs derived from the location section
when the profile leaves them out (and the committed explicit lists kept); a
disabled source is never imported or planned; `main.py cron` for the
committed profile and for a Denver profile, including a lead that crosses
midnight and a line that would land on the hour; daily.yml and
weekly_summary.yml equal what `main.py cron` generates (PUB-08); the hard_block map
stores the tier entry from both paths; status_email and emailer render
another candidate. No network access.
"""

import copy
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import emailer  # noqa: E402
import profile_loader  # noqa: E402
import schedule  # noqa: E402
import scorer  # noqa: E402
import sheet_dates  # noqa: E402
import weekly_summary  # noqa: E402
from sources import status_email  # noqa: E402


def _raw(**sections):
    data = copy.deepcopy(profile_loader.read())
    for section, fields in sections.items():
        data[section].update(fields)
    return data


def _profile(**sections):
    data = _raw(**sections)
    assert profile_loader.validate(data) == []
    return profile_loader._freeze(data)


DENVER = {"home": "Denver, CO", "commutable": ["Denver, CO", "Salt Lake City, UT"],
          "relocate": ["Austin, TX"], "state_tier_overrides": {}}


def _without(data, section, key):
    del data["sources"][section][key]
    return data


# --- Derived source lists ----------------------------------------------------------


def test_a_denver_profile_derives_adzuna_locations_and_builtin_urls():
    data = _raw(location=DENVER)
    _without(data, "adzuna", "locations")
    _without(data, "builtin", "search_urls")
    assert profile_loader.validate(data) == []
    p = profile_loader._freeze(data)
    assert config.adzuna_locations(p) == ["Denver", "Salt Lake City", "Remote"]
    # Built In has no Denver site: remote only, each title query in order.
    assert config.builtin_search_urls(p) == [
        "https://builtin.com/jobs/remote?search=director+IT&seniority[]=senior-leader",
        "https://builtin.com/jobs/remote?search=VP+IT&seniority[]=senior-leader",
    ]


def test_a_home_metro_with_a_built_in_site_gets_it_first():
    data = _raw(location={"home": "Seattle, WA", "commutable": ["Seattle, WA"],
                          "relocate": [], "state_tier_overrides": {}})
    _without(data, "builtin", "search_urls")
    data["sources"]["builtin"]["title_queries"] = ["Head of IT"]
    p = profile_loader._freeze(data)
    assert config.builtin_search_urls(p) == [
        "https://builtin.com/jobs/seattle?search=Head+of+IT&seniority[]=senior-leader",
        "https://builtin.com/jobs/remote?search=Head+of+IT&seniority[]=senior-leader",
    ]


def test_the_committed_explicit_lists_are_kept():
    p = profile_loader.active()
    assert config.adzuna_locations() == ["Chicago", "Milwaukee", "Indianapolis", "Remote"]
    assert config.builtin_search_urls() == list(p["sources"]["builtin"]["search_urls"])
    # And the Built In derivation gives exactly those URLs.
    data = _without(copy.deepcopy(profile_loader.read()), "builtin", "search_urls")
    assert config.builtin_search_urls(profile_loader._freeze(data)) == list(p["sources"]["builtin"]["search_urls"])


def test_sources_read_their_settings_from_the_profile(monkeypatch):
    for name in ("ADZUNA_APP_ID", "ADZUNA_API_KEY", "RAPIDAPI_KEY", "USAJOBS_API_KEY"):
        monkeypatch.setenv(name, "test")
    from sources import adzuna, builtin, jsearch, linkedin_email, usajobs
    src = profile_loader.active()["sources"]
    assert adzuna.ADZUNA_QUERIES == list(src["adzuna"]["queries"])
    assert adzuna.LOCATIONS == ["Chicago", "Milwaukee", "Indianapolis", "Remote"]
    assert jsearch.PRIORITY_KEYWORDS == list(src["jsearch"]["queries"])
    assert builtin.SEARCH_URLS == list(src["builtin"]["search_urls"])
    assert linkedin_email.LINKEDIN_ALERT_FOLDER == src["linkedin_label"]
    assert usajobs.USAJOBS_JOB_SERIES == "2210"
    assert usajobs.USAJOBS_HIRING_PATHS == "public;ses"
    assert usajobs.USAJOBS_TITLE_KEYWORDS == tuple(src["usajobs"]["title_keywords"])


def test_config_holds_no_per_user_source_or_delivery_value():
    for name in ("RECIPIENT_EMAIL", "ADZUNA_QUERIES", "LOCATIONS", "LINKEDIN_ALERT_FOLDER",
                 "USAJOBS_JOB_SERIES", "USAJOBS_HIRING_PATHS", "USAJOBS_TITLE_KEYWORDS"):
        assert not hasattr(config, name), name


# --- sources.enabled -----------------------------------------------------------------


def test_a_disabled_source_is_never_imported(tmp_path):
    data = profile_loader.read()
    data["sources"]["enabled"]["adzuna"] = False
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ADZUNA_")}
    env.update(JOB_DIGEST_PROFILE=str(path), PYTHONPATH=str(ROOT), RAPIDAPI_KEY="test",
               ANTHROPIC_API_KEY="test", GMAIL_ADDRESS="t@example.com", GMAIL_APP_PASSWORD="t")
    code = ("import sys, main, config; "
            "print('imported' if 'sources.adzuna' in sys.modules else 'not imported'); "
            "print(','.join(n for n, _ in main.SOURCES)); "
            "print(','.join(main._enabled(config.DAILY_SOURCES)))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert "[adzuna] disabled in profile" in lines
    assert lines[-3] == "not imported"
    assert "adzuna" not in lines[-2].split(",") and "jsearch" in lines[-2].split(",")
    assert lines[-1] == "jsearch,linkedin_email,builtin,ats,watchlist,usajobs"


def test_wellfound_false_never_touches_the_module():
    out = subprocess.run([sys.executable, "-c", "import sys, main; print('sources.wellfound' in sys.modules)"],
                         cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT), "RAPIDAPI_KEY": "t",
                              "ADZUNA_APP_ID": "t", "ADZUNA_API_KEY": "t"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "False"


def test_enabled_sources_logs_and_skips(capsys):
    import main
    loaded = main.enabled_sources([("adzuna", "sources.nonexistent_module"), ("ats", "sources.ats")],
                                  {"adzuna": False, "ats": True, "wellfound": True})
    assert [k for k, _ in loaded] == ["ats"]
    out = capsys.readouterr().out
    assert "[adzuna] disabled in profile" in out
    assert "[wellfound] enabled in profile but not wired into main.SOURCES (parked)" in out


# --- Cron ----------------------------------------------------------------------------


def _schedule(delivery, today):
    # PUB-08: one line per UTC offset (cron_lines.py became schedule.py; no
    # "current" line any more, every line fires and the slot guard picks).
    return schedule.lines(delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES, start=today)


COMMITTED = profile_loader.active()["delivery"]


def test_the_profile_gives_one_cron_line_per_offset():
    s = _schedule(COMMITTED, date(2026, 10, 28))
    assert [l.cron for l in s["digest"]] == ["17 8 * * 0-5", "17 7 * * 0-5"]   # standard, daylight
    assert [l.cron for l in s["weekly"]] == ["17 14 * * 6", "17 13 * * 6"]
    assert _schedule(COMMITTED, date(2026, 12, 1)) == s


def test_a_denver_0730_digest():
    d = dict(COMMITTED, timezone="America/Denver", digest_time_local="07:30")
    s = _schedule(d, date(2026, 10, 28))
    # 07:30 - 5 h 43 min = 01:47 local: 07:47 UTC in MDT, 08:47 UTC in MST.
    assert [l.cron for l in s["digest"]] == ["47 8 * * 0-5", "47 7 * * 0-5"]
    assert [l.cron for l in s["weekly"]] == ["17 15 * * 6", "17 14 * * 6"]


def test_a_lead_that_crosses_midnight_moves_the_days():
    d = dict(COMMITTED, timezone="America/Denver", digest_time_local="04:00",
             digest_days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])
    # 22:17 local the evening before (Sun-Thu) = 04:17 / 05:17 UTC on Mon-Fri.
    assert [l.cron for l in _schedule(d, date(2026, 10, 28))["digest"]] == ["17 5 * * 1-5", "17 4 * * 1-5"]


def test_a_line_on_the_hour_fires_earlier():
    d = dict(COMMITTED, digest_time_local="06:43")   # 01:00 local = 06:00 UTC in CDT
    assert "57 5 * * 0-5" in [l.cron for l in _schedule(d, date(2026, 10, 28))["digest"]]


def test_a_zone_without_daylight_time_has_one_line():
    d = dict(COMMITTED, timezone="America/Phoenix")
    s = _schedule(d, date(2026, 10, 28))
    assert [l.cron for l in s["digest"]] == ["17 9 * * 0-5"]


def _repo_profile():
    """The profile the repo's own workflow files were generated from: the
    maintainer's where it exists, the example in a public export."""
    path = ROOT / "profile.yaml"
    return path if path.exists() else ROOT / "profile.example.yaml"


def _repo_workflows():
    return {"digest": ROOT / ".github" / "workflows" / "daily.yml",
            "weekly": ROOT / ".github" / "workflows" / "weekly_summary.yml"}


def test_the_workflows_equal_what_main_py_cron_generates():
    # PUB-08: exact match, so a hand edit or a profile change fails here.
    delivery = profile_loader.load(str(_repo_profile()))["delivery"]
    stale = schedule.mismatches(delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES,
                                workflows=_repo_workflows())
    assert not stale, f"{', '.join(stale)} schedule block out of date: run python main.py cron --write"


def test_main_py_cron_and_profile_check_print_the_lines():
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["JOB_DIGEST_PROFILE"] = str(_repo_profile())
    delivery = profile_loader.load(env["JOB_DIGEST_PROFILE"])["delivery"]
    s = _schedule(delivery, date.today())
    digest, weekly = [l.cron for l in s["digest"]], [l.cron for l in s["weekly"]]
    out = subprocess.run([sys.executable, "main.py", "cron"], cwd=ROOT, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert all(f"- cron: '{line}'" in out.stdout for line in digest + weekly)
    assert "-> the file matches" in out.stdout
    check = subprocess.run([sys.executable, "main.py", "profile-check"], cwd=ROOT, capture_output=True,
                           text=True, env=env)
    assert check.returncode == 0, check.stderr
    quoted = lambda lines: ", ".join(f"'{line}'" for line in lines)
    assert f"Cron:        daily.yml {quoted(digest)} (matches)" in check.stdout
    assert f"Cron:        weekly_summary.yml {quoted(weekly)} (matches)" in check.stdout


# --- Hard-block map --------------------------------------------------------------------


def test_the_pre_filter_stores_the_tier_entry():
    job = scorer.score_jobs([{"title": "Director of IT", "company": "Lucky Casino Resorts"}])[0]
    assert job["score"] == 0 and job["hard_block_industry"] == "gambling/casinos"
    assert job["rationale"] == "Auto-disqualified: gambling/casinos industry"
    assert scorer._is_hard_disqualify({"title": "VP IT", "company": "Acme Sportsbook"}) == "gambling/casinos"
    assert scorer._is_hard_disqualify({"title": "VP IT", "company": "Acme Insurance"}) == "insurance"


def test_the_scoring_path_stores_the_tier_entry():
    assert scorer._hard_block_industry({"hard_block_industry": "Gambling/Casinos"}, {}) == "gambling/casinos"


def test_hard_block_map_validation():
    data = _raw()
    data["industries"]["hard_block"]["gambling/casinos"].append("insurance")
    assert any("keyword 'insurance' is also listed under 'insurance'" in e for e in profile_loader.validate(data))
    data = _raw()
    data["industries"]["hard_block_name_keywords"] = ["insurance"]
    assert "industries.hard_block_name_keywords: unknown field (not in the profile schema; a typo?)" \
        in profile_loader.validate(data)
    data = _raw()
    data["industries"]["hard_block"]["insurance"] = []
    assert "industries.hard_block.insurance: must list at least one entry" in profile_loader.validate(data)


# --- Another candidate --------------------------------------------------------------------


def test_status_email_names_another_candidate():
    p = _profile(candidate={"display_name": "Alex Rivera", "short_name": "Alex", "pronouns": "they/them"})
    prompt = status_email.status_prompt(p)
    assert prompt.startswith("You read one email from Alex Rivera's inbox")
    assert "tells Alex that THEIR OWN" in prompt and "invites them to an interview" in prompt
    assert "emails they\n  sent" in prompt
    assert not re.search(r"\b(she|her)\b", prompt)
    # The committed prompt is what TRK-12's cache was keyed on.
    assert status_email.SYSTEM_PROMPT == status_email.status_prompt(profile_loader.active())


def test_emailer_uses_the_profiles_address_and_zone(monkeypatch):
    monkeypatch.setattr(emailer, "RECIPIENT_EMAIL", "alex@example.com")
    html = emailer._build_html([], [], [], [], False, False)
    assert "Job Digest Agent · alex@example.com · All jobs logged" in html
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(emailer, "_TZ", ZoneInfo("America/Denver"))
    monkeypatch.setattr(emailer, "_TZ_LABEL", config.TZ_LABELS.get("America/Denver"))
    assert emailer._fetch_run_time_et("2026-10-06T04:17:00Z") == "Mon Oct 5, 10:17 PM MT"
    monkeypatch.setattr(emailer, "_TZ", ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr(emailer, "_TZ_LABEL", None)
    assert emailer._fetch_run_time_et("2026-10-06T04:17:00Z") == "Tue Oct 6, 6:17 AM CEST"


# --- Delivery ----------------------------------------------------------------------------


def test_delivery_timezone_drives_today_and_the_fresh_flag():
    assert str(sheet_dates.DIGEST_TZ) == "America/Chicago" == str(profile_loader.local_tz())
    late = datetime(2026, 10, 28, 3, 30, tzinfo=timezone.utc)       # 22:30 CDT on the 27th
    assert profile_loader.local_today(now=late) == date(2026, 10, 27)
    denver = _profile(delivery={"timezone": "America/Denver"})
    assert profile_loader.local_today(denver, now=datetime(2026, 10, 28, 5, 30, tzinfo=timezone.utc)) \
        == date(2026, 10, 27)


def test_the_weekly_summary_week_ends_the_day_before_its_day():
    assert weekly_summary.FRIDAY == 4                     # Saturday summary: Friday week end
    start, end = weekly_summary.week_bounds(date(2026, 10, 31))
    assert (start, end) == (date(2026, 10, 24), date(2026, 10, 30))
