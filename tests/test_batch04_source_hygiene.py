"""BATCH-04 source hygiene: Wellfound parked, Workday failures recorded without
raising, parked watchlist entries skipped, postings-vs-matches logging, and the
GAP-02 apply-link classifier."""

import ast
import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import requests

import config
from sources import ats, startups, watchlist


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error", response=self)


def _workday_post(failing_host):
    """Fake requests.post: the failing tenant 503s, every other returns one IT posting."""
    def post(url, json=None, **kw):
        if failing_host in url:
            return _Resp(503)
        return _Resp(200, {"jobPostings": [
            {"title": "Director of IT", "externalPath": "/job/x/Director-of-IT_1"},
            {"title": "Account Executive", "externalPath": "/job/x/AE_2"},
        ]})
    return post


def _no_network(monkeypatch, failing_host):
    monkeypatch.setattr(watchlist.requests, "post", _workday_post(failing_host))
    monkeypatch.setattr(watchlist, "_workday_detail", lambda *a: ("desc", "Remote, US", "remote", ""))
    monkeypatch.setattr(watchlist.time, "sleep", lambda s: None)


# --- GAP-05 ------------------------------------------------------------------

def test_wellfound_not_called_or_gated():
    tree = ast.parse((ROOT / "main.py").read_text())
    sources = next(n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SOURCES" for t in n.targets))
    names = [elt.elts[0].value for elt in sources.elts]
    assert "wellfound" not in names
    assert "builtin" in names
    assert "wellfound" not in config.TITLE_GATED_SOURCES
    assert (ROOT / "sources" / "wellfound.py").exists()


# --- Workday failures ----------------------------------------------------------

def test_workday_error_marks_board_failed_without_raising(monkeypatch):
    _no_network(monkeypatch, "bad.wd5")
    result = ats.fetch_one_board("Bad Co", "workday", "bad:Site", "bad.wd5.myworkdayjobs.com/Site",
                                 source="Startup")
    assert result.error is not None
    assert list(result) == []


def test_one_tenant_5xx_run_continues_and_board_shows_failed(monkeypatch, capsys):
    _no_network(monkeypatch, "bad.wd5")
    boards = [
        {"company": "Bad Co", "ats": "workday", "slug": "bad:Site", "board_url": "bad.wd5.myworkdayjobs.com/Site"},
        {"company": "Good Co", "ats": "workday", "slug": "good:Site", "board_url": "good.wd5.myworkdayjobs.com/Site"},
    ]
    monkeypatch.setattr(startups, "load_boards", lambda: boards)
    monkeypatch.setattr(startups, "BOARD_DELAY", 0)

    jobs = startups.fetch_all(budget_secs=30, workers=1)

    out = capsys.readouterr().out
    assert [j["company"] for j in jobs] == ["Good Co"]
    assert "FAILED Bad Co (workday/bad:Site) [503]" in out
    assert "1 succeeded, 1 failed" in out
    assert "workday / 503: 1" in out


def test_watchlist_tenant_failure_logged_and_run_continues(monkeypatch, capsys):
    _no_network(monkeypatch, "bad.wd5")
    tenants = {"Bad Co": {"host": "bad.wd5.myworkdayjobs.com", "tenant": "bad", "site": "Site"},
               "Good Co": {"host": "good.wd5.myworkdayjobs.com", "tenant": "good", "site": "Site"}}
    monkeypatch.setattr(watchlist, "WORKDAY_TENANTS", tenants)
    monkeypatch.setattr(watchlist, "DREAM_COMPANY_CAREER_URLS", {})
    monkeypatch.setattr(watchlist, "_unresolved_companies", lambda: ["Bad Co", "Good Co"])
    monkeypatch.setattr(watchlist, "_parked_keys", lambda: set())

    jobs = watchlist.fetch_all()

    out = capsys.readouterr().out
    assert [j["company"] for j in jobs] == ["Good Co"]
    assert "Workday Bad Co: FAILED [503]" in out
    assert "1 failed" in out


# --- GAP-06: parked watchlist entries ------------------------------------------

def test_watchlist_skips_parked_tenants_and_career_urls(monkeypatch, capsys):
    tenants = {"Parked Tenant": {"host": "p.wd1.myworkdayjobs.com", "tenant": "p", "site": "S"},
               "Live Tenant": {"host": "l.wd1.myworkdayjobs.com", "tenant": "l", "site": "S"}}
    urls = {"Parked Page": "https://parked.example/jobs", "Live Page": "https://live.example/jobs"}
    monkeypatch.setattr(watchlist, "WORKDAY_TENANTS", tenants)
    monkeypatch.setattr(watchlist, "DREAM_COMPANY_CAREER_URLS", urls)
    monkeypatch.setattr(watchlist, "_unresolved_companies", lambda: list(tenants) + list(urls))
    monkeypatch.setattr(watchlist, "_parked_keys",
                        lambda: {("workday", "p:S"), ("careers_page", "Parked Page")})
    monkeypatch.setattr(watchlist.time, "sleep", lambda s: None)
    fetched = []
    monkeypatch.setattr(watchlist, "_fetch_workday",
                        lambda c, cfg: fetched.append(c) or ats.BoardResult(postings=0))
    monkeypatch.setattr(watchlist, "_check_company", lambda c, u: fetched.append(c) or [])

    watchlist.fetch_all()

    assert sorted(fetched) == ["Live Page", "Live Tenant"]
    assert "2 parked, not fetched" in capsys.readouterr().out


def test_parked_csv_rows_are_well_formed():
    with (ROOT / "sourcing" / "parked_boards.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert all(r["reason"] and r["parked_on"] and r["last_checked"] for r in rows)


# --- postings vs matches -------------------------------------------------------

def test_posting_count_reads_every_vendor_shape():
    assert ats.posting_count("breezy", [{}, {}, {}]) == 3
    assert ats.posting_count("teamtailor", {"items": [{}, {}]}) == 2
    assert ats.posting_count("greenhouse", {"jobs": [{}]}) == 1
    assert ats.posting_count("bamboohr", {"result": [{}, {}]}) == 2
    assert ats.posting_count("smartrecruiters", {"content": [{}]}) == 1
    assert ats.posting_count("recruitee", {"offers": [{}, {}]}) == 2
    assert ats.posting_count("oracle_hcm", {"items": [{"requisitionList": [{}, {}]}, {"requisitionList": [{}]}]}) == 3


def test_fetch_one_board_reports_postings_before_gate(monkeypatch):
    payload = [{"name": "Director of IT", "location": {}}, {"name": "Sales Assistant", "location": {}},
               {"name": "Talents Pool", "location": {}}]
    monkeypatch.setattr(ats, "get_with_retry", lambda url: _Resp(200, payload))
    result = ats.fetch_one_board("Co", "breezy", "co", "https://co.breezy.hr/json", source="Startup")
    assert result.postings == 3
    assert len(result) == 1


def test_startup_log_has_one_postings_line_per_vendor(monkeypatch, capsys):
    boards = [{"company": "A", "ats": "breezy", "slug": "a", "board_url": "u"},
              {"company": "B", "ats": "teamtailor", "slug": "b", "board_url": "u"}]
    monkeypatch.setattr(startups, "load_boards", lambda: boards)
    monkeypatch.setattr(startups, "BOARD_DELAY", 0)
    monkeypatch.setattr(startups, "fetch_one_board",
                        lambda *a, **k: ats.BoardResult(postings=7 if a[1] == "breezy" else 4))
    startups.fetch_all(budget_secs=30, workers=1)
    out = capsys.readouterr().out
    assert "[Startup] postings: breezy           fetched=7      passed title gate=0" in out
    assert "[Startup] postings: teamtailor       fetched=4      passed title gate=0" in out


# --- GAP-02 classifier ---------------------------------------------------------

def test_apply_link_classifier():
    from url_resolver import link_class as classify
    assert classify("https://job-boards.greenhouse.io/axon/jobs/1") == "ats"
    assert classify("https://nvidia.wd5.myworkdayjobs.com/en-US/X/job/1") == "ats"
    assert classify("https://careers.example.com/job/1") == "company"
    assert classify("https://www.linkedin.com/jobs/view/123") == "linkedin"
    assert classify("https://www.indeed.com/viewjob?jk=1") == "job board"
    assert classify("https://builtin.com/job/1") == "job board"
    assert classify("https://www.adzuna.com/land/ad/1") == "aggregator"
    assert classify("") == "none"


def test_best_link_follows_prd_priority():
    from url_resolver import best_link
    li = "https://www.linkedin.com/jobs/view/director-it-at-harvey-4453759200"
    jooble = "https://jooble.org/jdp/4918580944500890853"
    zip_ = "https://www.ziprecruiter.com/c/Walmart/Job/x"
    company = "https://careers.mastercard.com/us/en/job/R-290882"
    assert best_link([jooble, zip_, li, company]) == company
    assert best_link([jooble, zip_, li]) == li
    assert best_link([jooble, zip_]) == zip_
    assert best_link([jooble, ""]) == jooble
    assert best_link([]) == ""


def test_jsearch_prefers_employer_apply_option(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", "test")
    from sources import jsearch
    r = {"job_title": "Director of IT", "employer_name": "Harvey",
         "job_apply_link": "https://www.linkedin.com/jobs/view/director-it-at-harvey-4453759200",
         "apply_options": [
             {"publisher": "LinkedIn", "apply_link": "https://www.linkedin.com/jobs/view/director-it-at-harvey-4453759200"},
             {"publisher": "Jooble", "apply_link": "https://jooble.org/jdp/1"},
             {"publisher": "Harvey", "apply_link": "https://jobs.ashbyhq.com/harvey/abc", "is_direct": True},
         ]}
    assert jsearch._normalize(r)["apply_link"] == "https://jobs.ashbyhq.com/harvey/abc"
    assert jsearch._normalize({"job_apply_link": "https://jooble.org/jdp/1"})["apply_link"] == "https://jooble.org/jdp/1"
