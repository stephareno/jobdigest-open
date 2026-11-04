"""DEL-05: posted dates and freshness (PRD §7).

Each vendor's first-posted field, full timestamps normalized to UTC, a
date-only source, a relative-text source (Built In), an empty date, Oracle HCM
(dated but never fresh), the Sheet's Posted Date cell, and the digest's age
text, fresh flag and ordering. No network access.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("RAPIDAPI_KEY", "x")
os.environ.setdefault("ADZUNA_APP_ID", "x")
os.environ.setdefault("ADZUNA_API_KEY", "x")

import sheet_dates as sd  # noqa: E402
from sources import ats, builtin, watchlist  # noqa: E402

IT = "Director of IT"
# 2026-09-16 12:00 UTC = 08:00 ET, digest time.
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def one(ats_name, data, slug="acme"):
    jobs = list(ats.parse_board("Acme", ats_name, slug, data))
    assert len(jobs) == 1
    return jobs[0]


# ---------------------------------------------------------------- normalize

@pytest.mark.parametrize("raw, expected", [
    ("2026-08-21T21:32:54-04:00", ("2026-08-22T01:32:54Z", "time")),
    ("2026-08-24T14:44:49.699+00:00", ("2026-08-24T14:44:49Z", "time")),
    ("2021-12-15T22:57:25.000Z", ("2021-12-15T22:57:25Z", "time")),
    ("2023-02-20 19:40:54 UTC", ("2023-02-20T19:40:54Z", "time")),
    ("2026-09-15T10:00:00", ("2026-09-15T10:00:00Z", "time")),  # no offset: UTC
    (1779920530389, ("2026-05-27T22:22:10Z", "time")),           # epoch ms
    ("2026-09-15", ("2026-09-15", "date")),
    ("", ("", "")),
    (None, ("", "")),
    ("Posted 30+ Days Ago", ("", "")),
])
def test_normalize_posted(raw, expected):
    assert sd.normalize_posted(raw) == expected


def test_normalize_posted_date_only_keeps_the_source_date():
    assert sd.normalize_posted("2026-09-16T00:00:00.000Z", date_only=True) == ("2026-09-16", "date")


# ---------------------------------------------------------------- vendors

def test_greenhouse_reads_first_published_not_updated_at():
    job = one("greenhouse", {"jobs": [{
        "title": IT, "absolute_url": "https://x/1", "location": {"name": "Remote"},
        "first_published": "2026-09-15T09:00:00-04:00", "updated_at": "2026-09-16T08:00:00-04:00"}]})
    assert job["date_posted"] == "2026-09-15T13:00:00Z"
    assert job["posted_precision"] == "time"
    assert job["ats"] == "greenhouse"


def test_lever_created_at_epoch_ms():
    job = one("lever", [{"text": IT, "hostedUrl": "https://x/1", "createdAt": 1779920530389}])
    assert job["date_posted"] == "2026-05-27T22:22:10Z"


def test_ashby_published_at():
    job = one("ashby", {"jobs": [{"title": IT, "jobUrl": "https://x/1",
                                  "publishedAt": "2026-08-24T14:44:49.699+00:00"}]})
    assert (job["date_posted"], job["posted_precision"]) == ("2026-08-24T14:44:49Z", "time")


def test_smartrecruiters_released_date():
    job = one("smartrecruiters", {"content": [{"name": IT, "id": "1",
                                               "releasedDate": "2026-09-01T10:00:00.000Z"}]})
    assert job["date_posted"] == "2026-09-01T10:00:00Z"


def test_workable_published_on_only():
    job = one("workable", {"jobs": [{"title": IT, "url": "https://x/1",
                                     "published_on": "2026-07-31", "created_at": "2025-03-28"}]})
    assert (job["date_posted"], job["posted_precision"]) == ("2026-07-31", "date")
    undated = one("workable", {"jobs": [{"title": IT, "url": "https://x/2", "created_at": "2025-03-28"}]})
    assert undated["date_posted"] == ""  # never falls back to created_at


def test_recruitee_published_at_not_created_at():
    job = one("recruitee", {"offers": [{"title": IT, "careers_url": "https://x/1",
                                        "created_at": "2021-01-20 20:57:39 UTC",
                                        "published_at": "2023-02-20 19:40:54 UTC"}]})
    assert job["date_posted"] == "2023-02-20T19:40:54Z"


def test_breezy_and_teamtailor():
    b = one("breezy", [{"name": IT, "url": "https://x/1", "published_date": "2026-08-27T20:33:43.362Z"}])
    assert b["date_posted"] == "2026-08-27T20:33:43Z"
    t = one("teamtailor", {"items": [{"title": IT, "url": "https://x/2",
                                      "date_published": "2026-09-14T09:29:12+02:00"}]})
    assert t["date_posted"] == "2026-09-14T07:29:12Z"


def test_rippling_created_on_from_detail(monkeypatch):
    monkeypatch.setattr(ats, "_rippling_detail", lambda slug, uuid: ("desc", "2026-08-31T03:22:25.616000-07:00"))
    monkeypatch.setattr(ats, "BOARD_DELAY", 0)
    job = one("rippling", [{"name": IT, "uuid": "u1", "url": "https://x/1"}])
    assert (job["description"], job["date_posted"]) == ("desc", "2026-08-31T10:22:25Z")


def test_bamboohr_date_posted_from_detail(monkeypatch):
    monkeypatch.setattr(ats, "_bamboohr_detail", lambda slug, jid: ("desc", "https://share", "2026-09-10"))
    monkeypatch.setattr(ats, "BOARD_DELAY", 0)
    job = one("bamboohr", {"result": [{"jobOpeningName": IT, "id": "7"}]})
    assert (job["date_posted"], job["posted_precision"]) == ("2026-09-10", "date")


def test_workday_start_date_from_detail(monkeypatch):
    monkeypatch.setattr(watchlist, "_workday_detail", lambda *a: ("desc", "Remote, US", "remote", "2026-09-15"))
    monkeypatch.setattr(watchlist.time, "sleep", lambda s: None)

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jobPostings": [{"title": IT, "externalPath": "/job/1", "postedOn": "Posted Yesterday"}]}

    monkeypatch.setattr(watchlist.requests, "post", lambda *a, **k: Resp())
    jobs = watchlist.fetch_workday("Acme", "acme.wd5.myworkdayjobs.com", "acme", "Ext")
    assert [(j["date_posted"], j["posted_precision"], j["ats"]) for j in jobs] == [("2026-09-15", "date", "workday")]


def test_comeet_edited_posting_is_not_fresh():
    # Comeet's only timestamp is time_updated, bumped by any edit: never a posted date.
    job = one("comeet", [{"name": IT, "url_active_page": "https://x/1",
                          "time_updated": "2026-09-16T11:00:00Z"}])
    assert (job["date_posted"], job["posted_precision"]) == ("", "")
    assert not sd.is_fresh(job, NOW)
    assert sd.posted_age_text(job, NOW) == ""


def test_empty_date_stays_empty():
    job = one("ashby", {"jobs": [{"title": IT, "jobUrl": "https://x/1"}]})
    assert (job["date_posted"], job["posted_precision"]) == ("", "")
    assert sd.posted_age_text(job, NOW) == ""
    assert not sd.is_fresh(job, NOW)
    assert sd.posted_date_cell(job) == ""


# ---------------------------------------------------------------- Built In relative text

@pytest.mark.parametrize("text, expected", [
    ("Director of IT · Remote · 40 Minutes Ago", ("2026-09-16T11:20:00Z", "time")),
    ("3 Hours Ago", ("2026-09-16T09:00:00Z", "time")),
    ("an hour ago", ("2026-09-16T11:00:00Z", "time")),
    ("Yesterday", ("2026-09-15", "date")),
    ("Today", ("2026-09-16", "date")),
    ("4 Days Ago", ("2026-09-12", "date")),
    ("2 Weeks Ago", ("2026-09-02", "date")),
    ("Reposted Yesterday", ("", "")),
    ("Reposted 3 Days Ago", ("", "")),
    ("2 Months Ago", ("", "")),
    ("", ("", "")),
])
def test_parse_relative_posted(text, expected):
    assert sd.parse_relative_posted(text, now=NOW) == expected


def test_builtin_card_gets_relative_date(monkeypatch):
    html = ('<li class="job-card"><h2>Director of IT</h2><div class="company">Acme</div>'
            '<div class="location">Remote</div><a href="/job/director-it/1">x</a>'
            '<span>2 Hours Ago</span></li>')
    card = BeautifulSoup(html, "html.parser").select_one("li")
    job = builtin._parse_card(card)
    assert job["posted_precision"] == "time"
    assert job["date_posted"].endswith("Z")


# ---------------------------------------------------------------- sources with no date

def test_linkedin_has_no_date(monkeypatch):
    from sources import linkedin_email

    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(linkedin_email.requests, "get", boom)
    job = linkedin_email._fetch_job_details("123")
    assert (job["date_posted"], job["posted_precision"]) == ("", "")


# ---------------------------------------------------------------- age text and fresh flag

def job(posted, precision, **extra):
    return {"date_posted": posted, "posted_precision": precision, **extra}


def test_time_source_age_and_fresh():
    j = job("2026-09-16T07:00:00Z", "time")
    assert sd.posted_age_text(j, NOW) == "posted 5h ago"
    assert sd.is_fresh(j, NOW)
    old = job("2026-09-15T11:00:00Z", "time")  # 25h
    assert sd.posted_age_text(old, NOW) == "posted 25h ago"
    assert not sd.is_fresh(old, NOW)
    older = job("2026-09-10T12:00:00Z", "time")
    assert sd.posted_age_text(older, NOW) == "posted 6d ago"


def test_date_only_source_today_yesterday_and_days():
    assert sd.posted_age_text(job("2026-09-16", "date"), NOW) == "posted today"
    assert sd.posted_age_text(job("2026-09-15", "date"), NOW) == "posted yesterday"
    assert sd.posted_age_text(job("2026-09-12", "date"), NOW) == "posted 4d ago"
    assert sd.is_fresh(job("2026-09-16", "date"), NOW)
    assert sd.is_fresh(job("2026-09-15", "date"), NOW)
    assert not sd.is_fresh(job("2026-09-14", "date"), NOW)


def test_date_only_today_is_eastern_not_utc():
    # 02:00 UTC on the 17th is still the 16th in ET.
    late = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
    assert sd.posted_age_text(job("2026-09-16", "date"), late) == "posted today"


def test_oracle_hcm_dated_but_never_fresh():
    data = {"items": [{"requisitionList": [{"Title": IT, "Id": "9", "PostedDate": "2026-09-16"}]}]}
    j = one("oracle_hcm", data, slug="host.oraclecloud.com:CX")
    assert (j["date_posted"], j["ats"]) == ("2026-09-16", "oracle_hcm")
    assert sd.posted_age_text(j, NOW) == "posted today"
    assert sd.posted_date_cell(j) == "2026-09-16"
    assert not sd.is_fresh(j, NOW)


def test_posted_date_cell_is_iso_date():
    assert sd.posted_date_cell(job("2026-09-16T01:30:00Z", "time")) == "2026-09-16"
    assert sd.posted_date_cell(job("2026-09-15", "date")) == "2026-09-15"


# ---------------------------------------------------------------- ordering and HTML

def test_sections_sort_by_score_then_newest_then_undated():
    import scorer
    a = {"title": "a", "score": 8, **job("2026-09-10T00:00:00Z", "time")}
    b = {"title": "b", "score": 8, **job("2026-09-16T00:00:00Z", "time")}
    c = {"title": "c", "score": 8, **job("", "")}
    d = {"title": "d", "score": 9, **job("", "")}
    e = {"title": "e", "score": 8, **job("2026-09-15", "date")}
    main = scorer.filter_jobs([a, b, c, d, e])["main"]
    assert [x["title"] for x in main] == ["d", "b", "e", "a", "c"]


def test_digest_html_shows_age_and_fresh_flag(monkeypatch):
    import emailer
    monkeypatch.setattr(sd, "datetime", _FrozenDatetime)
    fresh = {"title": "Fresh role", "company": "A", "score": 8, "ats": "greenhouse",
             **job("2026-09-16T07:00:00Z", "time")}
    oracle = {"title": "Oracle role", "company": "B", "score": 7, "ats": "oracle_hcm",
              **job("2026-09-16", "date")}
    undated = {"title": "LinkedIn role", "company": "C", "score": 7, "source": "LinkedIn",
               **job("", "")}
    html = emailer._build_html([fresh, oracle, undated], [], [], [], False, False)
    cards = html.split("Fresh role")[0], html.split("Fresh role")[1].split("Oracle role")[0], html.split("Oracle role")[1]
    assert "posted 5h ago" in cards[0] and ">fresh<" in cards[0]
    assert "posted today" in cards[1] and ">fresh<" not in cards[1]
    assert "posted" not in cards[2].split("LinkedIn role")[0] and ">fresh<" not in cards[2]


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)
