"""OPS-12: a billing or auth failure aborts the fetch run before anything is recorded.

Covers: which Anthropic errors end the run (credit balance, 401, 403) and which
keep the old fallback (429, 5xx, timeouts, an ordinary 400); a fetch run with
no API credit raises on the first scoring call and leaves seen_jobs.json and
state/pending/ untouched, so the next run re-fetches and scores the same
postings; Bespoke extraction ends the run the same way instead of logging a
green api_errors stop; the CLI exits non-zero with a ::error:: annotation.
No network access.
"""

import importlib
import json
import os
import runpy
import sys
import types
from datetime import datetime
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY", "GOOGLE_SHEET_ID",
            "GOOGLE_SERVICE_ACCOUNT_JSON", "ADZUNA_APP_ID", "ADZUNA_API_KEY", "RAPIDAPI_KEY"):
    os.environ.setdefault(var, "test")

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

import anthropic  # noqa: E402

import config  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import pending  # noqa: E402
import scorer  # noqa: E402
import state  # noqa: E402
import sources.bespoke as bespoke  # noqa: E402
from llm import ApiAccessError, access_error_cause  # noqa: E402
from sources import startups  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402

CREDIT_MESSAGE = ("Your credit balance is too low to access the Anthropic API. "
                  "Please go to Plans & Billing to upgrade or purchase credits.")


def _api_error(cls, status, error_type, message):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return cls(message, response=response, body=body)


def credit_error():
    return _api_error(anthropic.BadRequestError, 400, "invalid_request_error", CREDIT_MESSAGE)


class FakeProvider(StubProvider):
    """A stub model provider (PUB-14): raises `error`, or answers with `reply`
    as JSON (or `text` as it is)."""

    def __init__(self, error=None, reply=None, text=None):
        def respond(call):
            if error is not None:
                raise error
            return text if text is not None else json.dumps(reply)
        super().__init__(respond, usage={"input_tokens": 1, "output_tokens": 1})


GOOD_SCORE = {"score": 8, "rationale": "Strong fit; mission read.", "is_insurance": False,
              "is_cool_company_wrong_role": False, "function_fit": "leads_department",
              "work_model": "Remote",
              "city": None, "state": None, "hiring_company": "Acme"}


# ------------------------------------------------------------ classification

@pytest.mark.parametrize("error", [
    credit_error(),
    _api_error(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key"),
    _api_error(anthropic.PermissionDeniedError, 403, "permission_error", "not allowed"),
])
def test_billing_and_auth_errors_end_the_run(error):
    assert access_error_cause(error)


@pytest.mark.parametrize("error", [
    _api_error(anthropic.RateLimitError, 429, "rate_limit_error", "slow down"),
    _api_error(anthropic.InternalServerError, 500, "api_error", "internal"),
    _api_error(anthropic.InternalServerError, 529, "overloaded_error", "Overloaded"),
    _api_error(anthropic.BadRequestError, 400, "invalid_request_error", "max_tokens: too large"),
    anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com")),
    ValueError("not an API error"),
])
def test_transient_and_other_errors_do_not(error):
    assert access_error_cause(error) == ""


# ------------------------------------------------------------------- scorer

def test_scorer_raises_on_credit_error(monkeypatch):
    monkeypatch.setattr(llm, "_provider", FakeProvider(error=credit_error()))
    with pytest.raises(ApiAccessError, match="credit balance"):
        scorer._score_single({"title": "Director of IT", "company": "Acme"})


def test_scorer_keeps_the_fallback_for_transient_errors(monkeypatch):
    overloaded = _api_error(anthropic.InternalServerError, 529, "overloaded_error", "Overloaded")
    monkeypatch.setattr(llm, "_provider", FakeProvider(error=overloaded))
    result = scorer._score_single({"title": "Director of IT", "company": "Acme"})
    assert result["score"] == 0 and result["rationale"] == "Scoring error"


# ---------------------------------------------------------------- fetch run

def _job(url, company):
    return {"title": "Director of IT", "company": company, "apply_link": url, "source": "Startup",
            "location": "Remote, US", "work_model": "remote", "description": "d"}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """A fetch run with every network edge stubbed except the scorer, which uses the real code."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path / "state" / "pending")
    (tmp_path / state.STATE_FILE).write_text(json.dumps({"seed": datetime.utcnow().isoformat()}))
    main.SOURCE_OK.clear()
    postings = [_job("https://boards.greenhouse.io/acme/jobs/1", "Acme"),
                _job("https://jobs.lever.co/beta/2", "Beta")]
    monkeypatch.setattr(main, "SOURCES", [(n, lambda: []) for n, _ in main.SOURCES])
    monkeypatch.setattr(main, "fetch_startups",
                        lambda slot=None: startups.FetchResult([dict(j) for j in postings], complete=True))
    monkeypatch.setattr(main, "load_connections", lambda: {})
    monkeypatch.setattr(main, "find_warm_contact", lambda company, connections: "")
    monkeypatch.setattr(main, "resolve_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_declined_feedback", lambda: [])
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    return tmp_path


def _queued(tmp_path):
    return sorted((tmp_path / "state" / "pending").glob("*")) if (tmp_path / "state" / "pending").exists() else []


def test_no_credit_aborts_before_anything_is_queued_or_marked_seen(pipeline, monkeypatch):
    seen_before = json.loads((pipeline / state.STATE_FILE).read_text())
    fake = FakeProvider(error=credit_error())
    monkeypatch.setattr(llm, "_provider", fake)

    with pytest.raises(ApiAccessError, match="credit balance"):
        main.fetch_run()

    assert len(fake.calls) == 1                                   # stopped at the first scoring call
    assert json.loads((pipeline / state.STATE_FILE).read_text()) == seen_before
    assert _queued(pipeline) == []
    assert not (pipeline / config.SHARD_STATE_FILE).exists()  # slot not recorded as done

    # Credit is back: the next run re-fetches and scores the same postings.
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    main.SOURCE_OK.clear()
    main.fetch_run()
    _, queued, _ = pending.load_pending()
    assert sorted(j["apply_link"] for j in queued) == ["https://boards.greenhouse.io/acme/jobs/1",
                                                      "https://jobs.lever.co/beta/2"]
    assert all(j["score"] == 8 for j in queued)


def _seen(tmp_path):
    return json.loads((tmp_path / state.STATE_FILE).read_text())


def test_transient_error_defers_three_times_then_records_score_0(pipeline, monkeypatch, capsys):
    overloaded = _api_error(anthropic.InternalServerError, 529, "overloaded_error", "Overloaded")
    monkeypatch.setattr(llm, "_provider", FakeProvider(error=overloaded))
    dkeys = {state.deferral_key(u) for u in ("https://boards.greenhouse.io/acme/jobs/1",
                                             "https://jobs.lever.co/beta/2")}
    for attempt in (1, 2, 3):
        main.SOURCE_OK.clear()
        main.fetch_run()                       # each run is served the same two postings
        assert pending.load_pending()[1] == []                  # nothing queued
        seen = _seen(pipeline)
        assert state.load_url_index(seen) == {"seed"}           # nothing marked seen
        assert state.load_content_index(seen) == {}
        assert {k: seen[k]["count"] for k in dkeys} == {k: attempt for k in dkeys}
        assert "[Scorer] deferred: Director of IT at Acme (InternalServerError: " in capsys.readouterr().out

    main.SOURCE_OK.clear()
    main.fetch_run()                           # fourth failure: recorded as today
    _, queued, _ = pending.load_pending()
    assert len(queued) == 2
    assert all(j["score"] == 0 and j["rationale"] == "Scoring error" for j in queued)
    assert all("scoring_error" not in j for j in queued)
    seen = _seen(pipeline)
    assert not dkeys & set(seen)                                # counters cleared
    assert len(state.load_url_index(seen)) == 3                 # seed + both postings


def test_a_deferred_posting_scored_on_retry_is_queued_and_its_counter_cleared(pipeline, monkeypatch):
    overloaded = _api_error(anthropic.InternalServerError, 529, "overloaded_error", "Overloaded")
    monkeypatch.setattr(llm, "_provider", FakeProvider(error=overloaded))
    main.fetch_run()
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    main.SOURCE_OK.clear()
    main.fetch_run()
    _, queued, _ = pending.load_pending()
    assert len(queued) == 2 and all(j["score"] == 8 for j in queued)
    assert state.load_deferrals(_seen(pipeline)) == {}


def test_a_non_transient_failure_is_recorded_at_once(pipeline, monkeypatch):
    monkeypatch.setattr(llm, "_provider", FakeProvider(text="not json"))
    main.fetch_run()
    _, queued, _ = pending.load_pending()
    assert len(queued) == 2 and all(j["rationale"] == "Scoring error" for j in queued)
    assert state.load_deferrals(_seen(pipeline)) == {}


# ------------------------------------------------------------------ Bespoke

def test_bespoke_extract_raises_on_credit_error(monkeypatch):
    monkeypatch.setattr(llm, "_provider", FakeProvider(error=credit_error()))
    with pytest.raises(ApiAccessError, match="Bespoke extraction failed"):
        bespoke.extract_jobs("Acme", "<html></html>", "https://x")


def test_bespoke_rolling_pass_ends_on_credit_error_not_api_errors_stop(monkeypatch):
    from test_bespoke import _checker, _companies
    provider = FakeProvider(error=credit_error())
    monkeypatch.setattr(llm, "_provider", provider)
    with pytest.raises(ApiAccessError):
        bespoke.run_rolling(_companies(20), {}, {}, budget_secs=1000, api_ready=True, check=_checker())
    # No more calls started once the first one came back with the error.
    assert len(provider.calls) <= bespoke.config.BESPOKE_EXTRACT_CONCURRENCY


def test_run_source_re_raises_instead_of_logging_a_failed_source():
    def bespoke_fetch():
        raise ApiAccessError("Bespoke extraction failed: credit balance too low (400): x")

    main.SOURCE_OK.clear()
    with pytest.raises(ApiAccessError):
        main.run_source("Bespoke", bespoke_fetch, fetch_deadline=float("inf"))


# ---------------------------------------------------------------------- CLI

def test_cli_exits_red_with_an_error_annotation(monkeypatch, capsys):
    def no_credit():
        raise ApiAccessError("scoring call failed: credit balance too low (400): x")

    monkeypatch.setattr(state, "is_first_run", no_credit)
    monkeypatch.setattr(sys, "argv", ["main.py", "fetch"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    assert exit_info.value.code == 1
    assert "::error title=Anthropic API access failed::scoring call failed: credit balance" in capsys.readouterr().out


# ------------------------------------------- Bespoke api_errors stop warning

def test_bespoke_api_errors_stop_warns_and_stays_green(monkeypatch, tmp_path, capsys):
    from test_bespoke import _async, _checker, _companies
    monkeypatch.setattr(bespoke, "STATE_PATH", tmp_path / "bespoke_state.json")
    monkeypatch.setattr(bespoke, "CURSOR_PATH", tmp_path / "bespoke_cursor.json")
    monkeypatch.setattr(bespoke, "load_companies", lambda: _companies(20))
    monkeypatch.setattr(bespoke, "_api_ready", lambda: True)
    monkeypatch.setattr(bespoke, "check_all", lambda batch, state: _async(_checker()(batch)))
    monkeypatch.setattr(bespoke, "extract_jobs",
                        lambda *a, **k: ([], 0, 0, "api_error: Error code: 529 - Overloaded\nretry later"))

    jobs = bespoke.fetch_all()               # returns normally: the run stays green
    out = capsys.readouterr().out
    assert jobs.warning.startswith(f"stopped after {bespoke.EXTRACTION_ERROR_STREAK} API errors")
    assert jobs.warning.endswith("last error: api_error: Error code: 529 - Overloaded retry later")
    assert f"::warning title=Bespoke extraction stopped::{jobs.warning}" in out

    main.SOURCE_OK.clear()
    main.SOURCE_WARNINGS.clear()
    main.run_source("Bespoke", lambda: jobs, fetch_deadline=float("inf"))
    assert main.SOURCE_WARNINGS == [("Bespoke", jobs.warning)]


def test_no_warning_without_an_api_errors_stop():
    run = bespoke.RollingRun()
    run.stop_reason = "budget"
    assert bespoke.api_errors_warning(run) == ""


def test_fetch_summary_prints_the_bespoke_warning(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    warned = bespoke.BespokeJobs([])
    warned.warning = "stopped after 5 API errors (5 in a row); last error: api_error: 529"
    monkeypatch.setattr(main, "SOURCES", [(n, (lambda: warned) if n == "bespoke" else (lambda: []))
                                          for n, _ in main.SOURCES])
    main.fetch_run(force_daily=True)
    out = capsys.readouterr().out
    summary = out[out.index("=== Warnings (this fetch run) ==="):]
    assert f"bespoke: {warned.warning}" in summary


# ------------------------------------------------- failed fetch-run banner

import emailer  # noqa: E402
from tools import fetch_run_outcomes  # noqa: E402

REPO = "example/job-digest-agent"
NOW = datetime.fromisoformat("2026-10-06T10:30:00+00:00")


def _run(run_id, started, updated, conclusion):
    return {"id": run_id, "run_started_at": started, "created_at": started, "updated_at": updated,
            "conclusion": conclusion, "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}


def fake_github():
    """A fake GitHub REST API: digest and fetch run lists, jobs and annotations."""
    api = fetch_run_outcomes.API
    routes = {
        f"{api}/repos/{REPO}/actions/workflows/daily.yml/runs": {"workflow_runs": [
            _run(900, "2026-10-05T09:40:00Z", "2026-10-05T09:44:00Z", "success"),
            _run(800, "2026-10-04T09:40:00Z", "2026-10-04T09:44:00Z", "success")]},
        f"{api}/repos/{REPO}/actions/workflows/fetch.yml/runs": {"workflow_runs": [
            _run(104, "2026-10-06T04:17:00Z", "2026-10-06T04:20:00Z", "failure"),
            _run(103, "2026-10-05T20:17:00Z", "2026-10-05T20:30:00Z", "success"),
            _run(102, "2026-10-05T12:17:00Z", "2026-10-05T12:59:00Z", "cancelled"),
            # Started before the last digest, finished after it: this digest reports it.
            _run(101, "2026-10-05T08:17:00Z", "2026-10-05T09:50:00Z", "failure"),
            # Already reported by the last digest.
            _run(100, "2026-10-05T04:17:00Z", "2026-10-05T04:40:00Z", "failure")]},
        f"{api}/repos/{REPO}/actions/runs/104/jobs": {"jobs": [{"id": 5104}]},
        f"{api}/repos/{REPO}/actions/runs/102/jobs": {"jobs": [{"id": 5102}]},
        f"{api}/repos/{REPO}/actions/runs/101/jobs": {"jobs": [{"id": 5101}]},
        f"{api}/repos/{REPO}/check-runs/5104/annotations": [
            {"annotation_level": "failure", "title": "", "message": "Process completed with exit code 1."},
            {"annotation_level": "failure", "title": "Anthropic API access failed",
             "message": "scoring call failed: credit balance too low (400): ..."}],
        f"{api}/repos/{REPO}/check-runs/5102/annotations": [
            {"annotation_level": "failure", "title": "", "message": "The operation was canceled."}],
        f"{api}/repos/{REPO}/check-runs/5101/annotations": [],
    }
    calls = []

    def get(url, params):
        calls.append(url)
        return routes[url]
    return get, calls


def test_collector_reports_every_fetch_run_since_the_last_digest():
    get, calls = fake_github()
    runs = fetch_run_outcomes.collect(get, REPO, current_run_id="999", now=NOW)
    assert [(r["run_id"], r["conclusion"]) for r in runs] == [
        (101, "failure"), (102, "cancelled"), (103, "success"), (104, "failure")]
    by_id = {r["run_id"]: r for r in runs}
    assert by_id[104]["error_title"] == "Anthropic API access failed"     # titled one wins
    assert by_id[102]["error_message"] == "The operation was canceled."
    assert by_id[101]["error_title"] == by_id[101]["error_message"] == ""
    assert by_id[104]["url"].endswith("/actions/runs/104")
    assert not any("runs/103/jobs" in c for c in calls)                   # successes cost no lookup


def test_collector_window_falls_back_to_48h_without_an_earlier_digest():
    get = lambda url, params: {"workflow_runs": [_run(999, "2026-10-06T09:00:00Z", "x", "success")]}
    since = fetch_run_outcomes.window_start(get, REPO, "999", NOW)       # only itself on record
    assert since == NOW - fetch_run_outcomes.FALLBACK_WINDOW


def test_banner_lists_each_failed_run_with_its_link():
    get, _ = fake_github()
    runs = fetch_run_outcomes.collect(get, REPO, current_run_id="999", now=NOW)
    failed = [r for r in runs if r["conclusion"] in main.FAILED_FETCH_CONCLUSIONS]
    html = emailer._build_html([], [], [], [], False, False, failed_fetch_runs=failed)
    assert ("Fetch run Mon Oct 5, 11:17 PM CT failed: Anthropic API access failed. "
            "Postings from that run were not scored.") in html
    assert "Fetch run Mon Oct 5, 7:17 AM CT failed: The operation was canceled." in html
    assert "Fetch run Mon Oct 5, 3:17 AM CT failed: failure, no error message." in html
    assert html.count("Postings from that run were not scored.") == 3
    assert f'href="https://github.com/{REPO}/actions/runs/104"' in html
    assert "No matches found today." in html                              # banner doesn't hide it


def test_no_failures_no_banner():
    html = emailer._build_html([], [], [], [], False, False, failed_fetch_runs=[])
    assert "Postings from that run were not scored" not in html


def test_digest_after_a_failed_fetch_run_carries_the_banner(pipeline, monkeypatch, tmp_path):
    get, _ = fake_github()
    runs_file = tmp_path / "fetch_runs.json"
    runs_file.write_text(json.dumps(fetch_run_outcomes.collect(get, REPO, "999", NOW)))
    monkeypatch.setenv("FETCH_RUNS_FILE", str(runs_file))

    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    main.fetch_run()                                                       # queue two postings
    sent = []
    monkeypatch.setattr(emailer, "_send", lambda subject, html: sent.append(html))
    monkeypatch.setattr(main, "send_digest", emailer.send_digest)
    monkeypatch.setattr(main, "append_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "append_disqualified_jobs", lambda jobs: None)
    monkeypatch.setitem(sys.modules, "sources.applied_doc", types.SimpleNamespace(seed_from_doc=lambda: None))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: None))
    main.digest_run()

    assert len(sent) == 1
    assert "failed: Anthropic API access failed." in sent[0]
    assert f"https://github.com/{REPO}/actions/runs/104" in sent[0]
    assert _queued(pipeline) == []                                         # still dequeued


def test_digest_without_the_runs_file_sends_without_a_banner(monkeypatch, tmp_path):
    monkeypatch.setenv("FETCH_RUNS_FILE", str(tmp_path / "missing.json"))
    assert main.load_failed_fetch_runs() == []
