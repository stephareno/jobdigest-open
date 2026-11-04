"""Runtime and delivery fixes from 2026-09-11: startup-tier budget, retry,
Cool-company cap, weekly-summary date parsing."""

import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import requests

from sheet_dates import parse_sheet_date
from sources import ats, startups


# --- sheet_dates -------------------------------------------------------------

def test_parse_sheet_date_handles_every_sheet_format():
    sep1 = date(2026, 9, 1)
    serial = (sep1 - date(1899, 12, 30)).days
    assert parse_sheet_date("2026-09-01") == sep1
    assert parse_sheet_date("9/1/2026") == sep1
    assert parse_sheet_date("2026-09-01T10:00:00") == sep1
    assert parse_sheet_date(serial) == sep1
    assert parse_sheet_date(str(serial)) == sep1
    assert parse_sheet_date("") is None
    assert parse_sheet_date(None) is None
    assert parse_sheet_date("not a date") is None


def test_text_comparison_bug_is_gone():
    # The old bug: "9/1/2026" >= "2026-08-28" as strings is True for every row.
    cutoff = date(2026, 8, 28)
    assert parse_sheet_date("8/1/2026") < cutoff
    assert parse_sheet_date("9/1/2026") >= cutoff


# --- startup tier budget -----------------------------------------------------

def _fake_boards(n):
    return [{"company": f"C{i}", "ats": "greenhouse", "slug": f"s{i}", "board_url": "u"}
            for i in range(n)]


def _patch(monkeypatch, n, per_board_secs, fail_every=0):
    monkeypatch.setattr(startups, "load_boards", lambda: _fake_boards(n))
    monkeypatch.setattr(startups, "read_parked", lambda: {})
    monkeypatch.setattr(startups, "BOARD_DELAY", 0)
    monkeypatch.setattr(startups, "IN_FLIGHT_GRACE_SECS", 1)

    def fake_fetch(company, ats_name, slug, url, source):
        time.sleep(per_board_secs)
        if fail_every and int(slug[1:]) % fail_every == 0:
            resp = requests.Response()
            resp.status_code = 403
            raise requests.HTTPError("403 Forbidden", response=resp)
        return [{"title": "VP of IT", "company": company}]

    monkeypatch.setattr(startups, "fetch_one_board", fake_fetch)


def test_startup_tier_finishes_within_budget_and_tags(monkeypatch, capsys):
    _patch(monkeypatch, 30, 0.01, fail_every=10)
    jobs = startups.fetch_all(budget_secs=30, workers=4)
    assert len(jobs) == 27  # boards 0, 10, 20 fail
    assert all(j["source"] == "startups" and j["tier"] == "startup" for j in jobs)
    out = capsys.readouterr().out
    assert "within budget" in out
    assert "greenhouse / 403: 3" in out


def test_startup_tier_stops_at_budget_and_reports_skipped(monkeypatch, capsys):
    _patch(monkeypatch, 60, 0.1)
    start = time.monotonic()
    jobs = startups.fetch_all(budget_secs=0.35, workers=3)
    assert time.monotonic() - start < 3
    assert 0 < len(jobs) < 60
    assert "BUDGET EXHAUSTED" in capsys.readouterr().out


# --- retry with backoff ------------------------------------------------------

def _resp(status):
    r = requests.Response()
    r.status_code = status
    r._content = b"{}"
    return r


def test_retry_recovers_from_transient_403(monkeypatch):
    calls = []
    replies = [_resp(403), _resp(403), _resp(200)]
    monkeypatch.setattr(ats.requests, "get", lambda url, **kw: calls.append(url) or replies.pop(0))
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    assert ats.get_with_retry("https://x").status_code == 200
    assert len(calls) == 3


def test_retry_does_not_retry_404(monkeypatch):
    calls = []
    monkeypatch.setattr(ats.requests, "get", lambda url, **kw: calls.append(url) or _resp(404))
    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    try:
        ats.get_with_retry("https://x")
        assert False, "404 should raise"
    except requests.HTTPError:
        pass
    assert len(calls) == 1


def test_browser_headers_are_sent():
    assert "Mozilla/5.0" in ats.HEADERS["User-Agent"]
    assert "application/json" in ats.HEADERS["Accept"]


# --- Cool company, wrong role cap (CLAUDE.md constraint #7) ------------------

def test_cool_company_section_is_capped_at_three():
    import emailer
    cool = [{"company": f"Co{i}", "title": "VP Sales", "apply_link": f"https://x/{i}", "score": i}
            for i in range(8)]
    html = emailer._build_html([], [], cool, [], False, False)
    assert html.count("View role") == 3
    assert "5 more logged to the Sheet only" in html
    assert "Co7" in html and "Co0" not in html  # highest scores kept
