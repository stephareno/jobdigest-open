"""REV-07: LinkedIn jobs whose page fails to load are dropped, not blanked.

Covers: a 999 response, a login-wall redirect, an exception and an empty
parse each drop the job unseen; a normal page is unchanged; the same job
failing three runs running is recorded at score 0 as "LinkedIn page
unavailable" and lands in the Disqualified tab; the per-run counts reach the
fetch summary; LINKEDIN_KEEP_ALERT_ONLY_ROWS keeps an alert-only row only when
set; the alert email's own title, company and location are read; the
digest's weekly line; doctor's LinkedIn WARN and its cached cost estimate.
No network access: requests.get is a stub.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import main, pending, pipeline, state, workdir  # noqa: E402,F401

import config  # noqa: E402
import doctor  # noqa: E402
import fetch_budget  # noqa: E402
import sources.linkedin_email as li  # noqa: E402

GOOD = ('<h1 class="top-card-layout__title">Director of IT</h1>'
        '<a class="topcard__org-name-link">Acme</a>'
        '<span class="topcard__flavor--bullet">Chicago, IL</span>'
        '<div class="description__text">Leads IT for a 900-person company. Remote.</div>')
NO_DESCRIPTION = '<h1 class="top-card-layout__title">Director of IT</h1>'


class Resp:
    def __init__(self, text="", status=200, url=None, history=()):
        self.text, self.status_code, self.url, self.history = text, status, url, list(history)


def _url(jid):
    return f"https://www.linkedin.com/jobs/view/{jid}/"


PAGES = {
    "100": lambda: Resp(GOOD, url=_url("100")),
    "999": lambda: Resp("", status=999, url=_url("999")),
    "201": lambda: Resp("<html>Sign in</html>", url="https://www.linkedin.com/authwall?trk=x",
                        history=[Resp(status=302, url=_url("201"))]),
    "300": lambda: (_ for _ in ()).throw(ConnectionError("connection reset")),
    "400": lambda: Resp(NO_DESCRIPTION, url=_url("400")),
}


@pytest.fixture
def linkedin(workdir, monkeypatch):
    """fetch_all over stub pages; returns a function running it for some ids."""
    monkeypatch.setattr(li.time, "sleep", lambda s: None)

    def get(url, headers=None, timeout=None):
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        return PAGES[jid]()
    monkeypatch.setattr(li.requests, "get", get)

    def run(ids, alerts=None, commit=True):
        monkeypatch.setattr(li, "_collect_job_ids_from_email",
                            lambda: (li.ALERT_INFO.update(alerts or {}), list(ids))[1])
        jobs = li.fetch_all()
        if commit:
            jobs.commit()
        return jobs
    return run


# ------------------------------------------------------------ one page

@pytest.mark.parametrize("jid, outcome", [("999", "blocked"), ("201", "login_wall"), ("300", "error"),
                                          ("400", "empty"), ("100", "ok")])
def test_each_page_outcome(linkedin, monkeypatch, jid, outcome):
    monkeypatch.setattr(li.requests, "get", lambda url, **k: PAGES[jid]())
    job, got = li._fetch_page(jid)
    assert got == outcome
    if outcome == "ok":
        assert job["title"] == "Director of IT" and job["company"] == "Acme" and job["description"]


@pytest.mark.parametrize("jid", ["999", "201", "300", "400"])
def test_a_failed_page_drops_the_job_unseen(linkedin, jid):
    jobs = linkedin([jid])
    assert list(jobs) == []
    assert not state.is_seen(_url(jid))                                       # a later run retries it
    assert state.load_linkedin_failures() == {state.linkedin_fail_key(_url(jid)): 1}


def test_a_normal_page_is_unchanged(linkedin):
    [job] = linkedin(["100"])
    assert job["title"] == "Director of IT" and job["company"] == "Acme" and job["location"] == "Chicago, IL"
    assert job["description"].startswith("Leads IT") and job["work_model"] == "Remote"
    assert "unavailable_reason" not in job
    assert state.load_linkedin_failures() == {}


def test_the_counts(linkedin, capsys):
    jobs = linkedin(["100", "999", "201", "300", "400"])
    assert jobs.counts == {"fetched": 1, "blocked": 1, "login_wall": 1, "empty": 1, "error": 1,
                           "kept": 0, "unavailable": 0}
    line = ("job pages: fetched 1, blocked 1, login wall 1, empty 1, error 1, kept 0, unavailable 0 "
            "(of 5 pages tried)")
    assert jobs.summary == line
    assert f"[LinkedIn Email] {line}" in capsys.readouterr().out
    [run] = fetch_budget.load()["linkedin_runs"]
    assert run["blocked"] == 1 and run["fetched"] == 1


def test_three_failed_runs_record_the_job_with_the_reason(linkedin):
    alerts = {"999": {"title": "VP, Information Technology", "company": "Initech", "location": "Austin, TX"}}
    assert list(linkedin(["999"], alerts)) == []
    assert list(linkedin(["999"], alerts)) == []
    [job] = linkedin(["999"], alerts)
    assert job["unavailable_reason"] == config.LINKEDIN_UNAVAILABLE_REASON == "LinkedIn page unavailable"
    assert (job["title"], job["company"], job["location"]) == ("VP, Information Technology", "Initech", "Austin, TX")
    assert job["description"] == ""
    assert state.load_linkedin_failures() == {}                               # cleared once recorded
    assert fetch_budget.load()["linkedin_runs"][-1]["unavailable"] == 1


def test_a_page_that_loads_again_clears_its_count(linkedin, monkeypatch):
    linkedin(["999"])
    PAGES_OK = dict(PAGES, **{"999": PAGES["100"]})
    monkeypatch.setattr(li.requests, "get", lambda url, **k: PAGES_OK[url.rstrip("/").rsplit("/", 1)[-1]]())
    [job] = linkedin(["999"])
    assert job["title"] == "Director of IT" and state.load_linkedin_failures() == {}


def test_nothing_is_saved_without_the_commit(linkedin):
    linkedin(["999"], commit=False)
    assert state.load_linkedin_failures() == {} and fetch_budget.load()["linkedin_runs"] == []


def test_keep_alert_only_rows_is_off_by_default_and_deliberate_when_on(linkedin, monkeypatch):
    alerts = {"999": {"title": "VP, IT", "company": "Initech", "location": "Austin, TX"}}
    assert config.LINKEDIN_KEEP_ALERT_ONLY_ROWS is False
    assert list(linkedin(["999"], alerts)) == []
    monkeypatch.setattr(config, "LINKEDIN_KEEP_ALERT_ONLY_ROWS", True)
    [job] = linkedin(["999"], alerts)
    assert (job["title"], job["company"], job["description"]) == ("VP, IT", "Initech", "")
    assert job.get("unavailable_reason") is None
    assert linkedin(["201"], {"201": {"title": "VP, IT"}}).counts["kept"] == 0    # needs all three fields


def test_alert_email_details_are_read_from_the_card():
    body = """<html><body><table>
      <tr><td><a href="https://www.linkedin.com/comm/jobs/view/4242/?trk=eml">Director of IT</a>
        <p>Initech · Austin, TX (Hybrid)</p></td></tr>
      <tr><td><a href="https://www.linkedin.com/jobs/view/4343/">VP, Technology</a>
        <p>Globex</p><p>Remote</p></td></tr>
      <tr><td><a href="https://www.linkedin.com/jobs/view/4343/">View job</a></td></tr>
    </table></body></html>"""
    got = li.alert_details(body)
    assert got["4242"] == {"title": "Director of IT", "company": "Initech", "location": "Austin, TX (Hybrid)"}
    assert got["4343"] == {"title": "VP, Technology", "company": "Globex", "location": "Remote"}
    assert li.alert_details("plain text, no html") == {}


# ------------------------------------------------------- through fetch_run

def _linkedin_source(monkeypatch, jobs):
    batch = li.LinkedInJobs(jobs, lambda: None, {"fetched": 0, "blocked": 3, "login_wall": 0, "empty": 0,
                                                 "error": 0, "kept": 0, "unavailable": len(jobs)})
    monkeypatch.setattr(main, "SOURCES", [(n, (lambda: batch) if n == "linkedin_email" else f)
                                          for n, f in main.SOURCES])


def test_a_recorded_job_reaches_the_disqualified_tab_with_the_reason(pipeline, monkeypatch, capsys):
    calls, catalog = pipeline
    job = {"title": "", "company": "", "location": "", "work_model": "", "comp": "", "description": "",
           "apply_link": _url("999"), "date_posted": "", "posted_precision": "", "source": "linkedin_email",
           "unavailable_reason": config.LINKEDIN_UNAVAILABLE_REASON}
    _linkedin_source(monkeypatch, [job])
    main.fetch_run(shard=None, force_daily=True)
    out = capsys.readouterr().out
    assert "=== Source counts (this fetch run) ===" in out
    assert "  linkedin_email: job pages: fetched 0, blocked 3" in out
    [queued] = pending.load_pending()[1]
    assert queued["score"] == 0 and queued["rationale"] == "LinkedIn page unavailable"
    assert state.is_seen(_url("999"))                                         # stops costing fetches
    main.digest_run()
    assert [j["rationale"] for j in calls["disq"]] == ["LinkedIn page unavailable"]
    assert calls["sheet"] == []


def test_the_digest_banner_counts_unreadable_jobs_this_week(pipeline, monkeypatch):
    calls, catalog = pipeline
    monkeypatch.setattr(fetch_budget, "today", lambda: main.profile_loader.local_today().isoformat())
    fetch_budget.record_linkedin({"fetched": 0, "blocked": 2, "unavailable": 2})
    catalog["Startup"] = [{"title": "Director of IT", "company": "Acme", "apply_link": "https://a.example/1",
                           "source": "Startup", "location": "Remote, US", "work_model": "remote",
                           "description": "d", "score": 8}]
    main.fetch_run(shard=None)
    notices = []
    monkeypatch.setattr(main, "send_digest", lambda sections, **kw: notices.append(kw["notices"]))
    main.digest_run()
    assert ("2 LinkedIn jobs could not be read this week: the page was blocked or behind a login wall "
            "three runs running, so they are in the Disqualified tab as \"LinkedIn page unavailable\".") \
        in notices[0]


# ------------------------------------------------------------------ doctor

@pytest.mark.parametrize("runs, status, text", [
    ([], "PASS", "no fetch run has tried"),
    ([{"fetched": 2, "blocked": 8}, {"fetched": 0, "blocked": 0}, {"fetched": 1, "blocked": 4},
      {"fetched": 0, "login_wall": 3}], "WARN", "LinkedIn blocked or hid 15 of 18 job pages over the last 3"),
    ([{"fetched": 5, "blocked": 5}], "PASS", "5 of 10 job pages read over the last 1"),
    ([{"fetched": 9, "blocked": 1}, {"fetched": 1, "empty": 2, "error": 1}], "PASS", "10 of 14"),
])
def test_doctor_linkedin_pages(workdir, monkeypatch, runs, status, text):
    monkeypatch.setattr(fetch_budget, "today", lambda: "2026-11-09")
    for run in runs:
        fetch_budget.record_linkedin(run)
    [got] = doctor.linkedin_pages()
    assert got["status"] == status and text in got["reason"], got
    if status == "WARN":
        assert "the alert-label reader still works, the page reader may not" in got["reason"]


def test_doctor_cost_estimate_uses_the_cache(tmp_path):
    tokens_in, tokens_out = config.SCORING_TOKENS_PER_CALL
    per_call, how = doctor.scoring_call_estimate("claude-sonnet-4-6", usage_file=tmp_path / "none.json")
    uncached = round(tokens_in * 0.15)
    assert per_call == pytest.approx(config.model_cost("claude-sonnet-4-6", uncached, tokens_out, 0,
                                                       tokens_in - uncached))
    assert how == "prompt cached, assumed 15% uncached input"
    assert per_call < config.model_cost("claude-sonnet-4-6", tokens_in, tokens_out)

    usage = tmp_path / "calibrate_usage.json"
    usage.write_text(json.dumps({"model": "claude-sonnet-4-6", "uncached": 3584, "cache_write": 2297,
                                 "cache_read": 18376}))
    per_call, how = doctor.scoring_call_estimate("claude-sonnet-4-6", usage_file=usage)
    assert how == "prompt cached, 15% uncached input in the last calibrate run"
    usage.write_text(json.dumps({"model": "claude-sonnet-4-6", "uncached": 500, "cache_write": 0,
                                 "cache_read": 500}))
    assert doctor.scoring_call_estimate("claude-sonnet-4-6", usage_file=usage)[1] == \
        "prompt cached, 50% uncached input in the last calibrate run"


def test_doctor_cost_estimate_without_a_cache_price(monkeypatch):
    monkeypatch.setitem(config.MODEL_PRICES, "x-model", (1.0, 2.0))
    per_call, how = doctor.scoring_call_estimate("x-model")
    assert how == "uncached" and per_call == pytest.approx(config.model_cost("x-model", *config.SCORING_TOKENS_PER_CALL))


def test_calibrate_saves_the_input_split(tmp_path):
    import calibrate
    path = tmp_path / "u.json"
    calibrate.save_usage({"model": "m", "calls": 9, "uncached": 3584, "cache_write": 2297, "cache_read": 18376,
                          "avg_input": 2695, "avg_output": 232, "per_call": 0.0063}, path=path)
    assert json.loads(path.read_text())["cache_read"] == 18376
    calibrate.save_usage({"calls": 0}, path=tmp_path / "none.json")
    assert not (tmp_path / "none.json").exists()
