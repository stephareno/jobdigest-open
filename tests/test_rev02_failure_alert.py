"""REV-02: failed fetch runs always produce an email, even on an empty day.

Covers: a failed run with an empty queue and skip_empty_days true sends one
alert-only email (subject, banner, sentence) counted as a skipped day; a
failed run with postings sends the normal digest with the banner; no
failures and an empty queue sends nothing, as before; a run is reported in
one sent email only, across digests and the weekly reset; a send that raises
reports nothing; run titles are HTML-escaped; the weekly summary counts the
alert emails; digest --dry-run --with-failed-runs renders both forms.
No network access.
"""

import json
import sys
import types
from datetime import date

import pytest

from tests.test_ops12_api_access_abort import (  # noqa: F401  (pipeline is a fixture)
    GOOD_SCORE, REPO, FakeProvider, _queued, pipeline)

import dry_run  # noqa: E402
import emailer  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import profile_loader  # noqa: E402
import state  # noqa: E402
import weekly_summary  # noqa: E402


def _failed(run_id, title="Anthropic API access failed", conclusion="failure",
            started="2026-10-06T04:17:00Z"):
    return {"run_id": run_id, "started_at": started, "conclusion": conclusion,
            "url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            "error_title": title, "error_message": "scoring call failed: credit balance too low"}


def _runs_file(tmp_path, runs, name="fetch_runs.json"):
    path = tmp_path / name
    path.write_text(json.dumps(runs))
    return path


@pytest.fixture
def digest(pipeline, monkeypatch):
    """digest_run with every network edge stubbed; returns the sent (subject, html) list."""
    sent = []
    monkeypatch.setattr(emailer, "_send", lambda subject, html: sent.append((subject, html)))
    monkeypatch.setattr(main, "send_digest", emailer.send_digest)
    monkeypatch.setattr(main, "send_alert_only", emailer.send_alert_only)
    monkeypatch.setattr(main, "append_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "append_disqualified_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_unseen_jobs", lambda: [])
    monkeypatch.setattr(main, "get_recent_index_rows", lambda days: [])
    monkeypatch.setitem(sys.modules, "sources.applied_doc", types.SimpleNamespace(seed_from_doc=lambda: None))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: None))
    # Both profiles the suite runs under turn empty-day skipping on (read-only mapping).
    assert profile_loader.active()["delivery"]["skip_empty_days"] is True
    return sent


def _skip_state(tmp_path):
    return json.loads((tmp_path / state.SKIP_STATE_FILE).read_text())


# --------------------------------------------------------------- digest_run

def test_failed_run_and_empty_queue_sends_one_alert_only_email(digest, pipeline, monkeypatch):
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()

    assert len(digest) == 1
    subject, html = digest[0]
    assert subject == "Job digest: fetch runs failed"
    when = emailer._fetch_run_time_et("2026-10-06T04:17:00Z")         # in the profile's zone
    assert f"Fetch run {when} failed: Anthropic API access failed." in html
    assert f'href="https://github.com/{REPO}/actions/runs/104"' in html
    assert emailer.ALERT_ONLY_NOTE in html
    assert "Job Digest Agent" in html and "All jobs logged in your Google Sheet" in html  # header, footer
    assert "No matches found today." not in html
    skip = _skip_state(pipeline)
    today = profile_loader.local_today().isoformat()
    assert skip["skipped_days"] == [today] and skip["run_days"] == [today]   # a skipped day
    assert skip["alert_emails"] == [today]
    assert skip["reported_fetch_runs"] == ["104"]


def test_failed_run_with_postings_sends_the_normal_digest_with_the_banner(digest, pipeline, monkeypatch):
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    main.fetch_run()                                                  # queue two postings
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()

    assert len(digest) == 1
    subject, html = digest[0]
    assert subject.startswith("Job digest — 1 match ·")              # one card (FIL-13 collapse)
    assert " failed: Anthropic API access failed." in html
    assert emailer.ALERT_ONLY_NOTE not in html
    skip = _skip_state(pipeline)
    assert skip["skipped_days"] == [] and skip["alert_emails"] == []
    assert skip["reported_fetch_runs"] == ["104"]
    assert _queued(pipeline) == []


def test_no_failures_and_empty_queue_sends_nothing(digest, pipeline, monkeypatch):
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [
        {**_failed(103), "conclusion": "success", "error_title": ""}])))
    main.digest_run()
    assert digest == []
    skip = _skip_state(pipeline)
    assert skip["skipped_days"] == [profile_loader.local_today().isoformat()]
    assert skip["alert_emails"] == [] and skip["reported_fetch_runs"] == []


def test_the_same_failed_run_is_reported_once_across_two_digests(digest, pipeline, monkeypatch):
    # The first digest sent, then (say) failed on a Sheet write, so the
    # collector's window did not move and the second sees run 104 again.
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()
    main.digest_run()
    assert len(digest) == 1

    # A new failure next to the old one: only the new one is reported.
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [
        _failed(104), _failed(105, title="Fetch budget exceeded", started="2026-10-06T12:17:00Z")])))
    main.digest_run()
    assert len(digest) == 2
    html = digest[1][1]
    assert "Fetch budget exceeded" in html and "Anthropic API access failed" not in html
    assert _skip_state(pipeline)["reported_fetch_runs"] == ["104", "105"]


def test_a_send_that_raises_reports_nothing(digest, pipeline, monkeypatch):
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))

    def broken(subject, html):
        raise OSError("smtp down")
    monkeypatch.setattr(emailer, "_send", broken)
    with pytest.raises(OSError):
        main.digest_run()
    assert state.unreported_fetch_runs([_failed(104)]) == [_failed(104)]   # the next digest reports it


def test_postings_with_no_matches_and_no_fallback_still_alert(digest, pipeline, monkeypatch):
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply={**GOOD_SCORE, "score": 1}))
    main.fetch_run()
    monkeypatch.setenv("FETCH_RUNS_FILE", str(_runs_file(pipeline, [_failed(104)])))
    main.digest_run()
    assert len(digest) == 1
    subject, html = digest[0]
    assert subject == "Job digest: fetch runs failed"
    assert emailer.ALERT_NO_MATCHES_NOTE in html and emailer.ALERT_ONLY_NOTE not in html
    skip = _skip_state(pipeline)
    assert skip["skipped_days"] == [profile_loader.local_today().isoformat()]   # nothing went out
    assert skip["alert_emails"] == [profile_loader.local_today().isoformat()]


# ------------------------------------------------------------------ banner

def test_run_titles_are_html_escaped():
    run = _failed(104, title='<script>alert("x")</script> & <b>bold</b>')
    run["url"] = 'javascript:alert(1)'
    _, html = emailer.build_alert_only([run])
    assert "<script>" not in html and "<b>bold</b>" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;" in html
    assert "javascript:" not in html                                    # not an href
    msg_only = {**_failed(105, title=""), "error_message": "<img src=x onerror=1>\nsecond line"}
    _, html = emailer.build_alert_only([msg_only])
    assert "<img" not in html and "&lt;img src=x onerror=1&gt;" in html and "second line" not in html


@pytest.mark.parametrize("conclusion, text", [
    ("failure", "failed"), ("cancelled", "was cancelled"),
    ("timed_out", "timed out"), ("startup_failure", "failed to start")])
def test_banner_names_each_runs_conclusion(conclusion, text):
    _, html = emailer.build_alert_only([_failed(104, title="", conclusion=conclusion) | {"error_message": ""}])
    assert f" {text}: no error message. Postings from that run were not scored." in html


def test_a_cause_ending_in_a_period_gets_one():
    run = {**_failed(105, title="", conclusion="cancelled"), "error_message": "The operation was canceled."}
    _, html = emailer.build_alert_only([run])
    assert "was cancelled: The operation was canceled. Postings from that run" in html


def test_alert_only_subject_takes_the_dry_run_prefix():
    subject, _ = emailer.build_alert_only([_failed(104)], subject_prefix=dry_run.SUBJECT_PREFIX)
    assert subject == "[DRY RUN] Job digest: fetch runs failed"


# ------------------------------------------------------- state and summary

def test_weekly_reset_keeps_the_reported_runs(pipeline):
    state.mark_fetch_runs_reported([_failed(104)])
    state.record_alert_email()
    state.record_run_day(had_new_jobs=False)
    assert state.get_alert_email_count() == 1
    state.reset_skip_stats()
    skip = _skip_state(pipeline)
    assert skip == {"run_days": [], "skipped_days": [], "alert_emails": [], "reported_fetch_runs": ["104"],
                    "reported_sheet_alerts": [], "reported_ceiling_days": []}
    assert state.unreported_fetch_runs([_failed(104)]) == []


def test_reported_runs_are_capped(pipeline):
    state.mark_fetch_runs_reported([_failed(i) for i in range(state.REPORTED_FETCH_RUNS_KEEP + 5)])
    ids = _skip_state(pipeline)["reported_fetch_runs"]
    assert len(ids) == state.REPORTED_FETCH_RUNS_KEEP and ids[-1] == str(state.REPORTED_FETCH_RUNS_KEEP + 4)


def test_old_skip_state_files_load(pipeline):
    (pipeline / state.SKIP_STATE_FILE).write_text(json.dumps({"run_days": ["2026-10-05"], "skipped_days": []}))
    assert state.get_alert_email_count() == 0
    assert state.unreported_fetch_runs([_failed(104)]) == [_failed(104)]


@pytest.mark.parametrize("alerts, text", [(0, "0 alert emails"), (1, "1 alert email sent"),
                                          (2, "2 alert emails")])
def test_weekly_summary_counts_the_alert_emails(alerts, text):
    out = weekly_summary.build_summary([], date(2026, 10, 10), skip_stats=(2, 6), alert_emails=alerts)
    assert f"2 of 6 run days quiet. {text}" in out
    assert "sent for failed fetch runs." in out


# ----------------------------------------------------------------- dry run

def test_dry_run_with_failed_runs_and_empty_queue_renders_the_alert(pipeline, monkeypatch, capsys):
    out = pipeline / "out" / "digest_dry_run.html"
    monkeypatch.setattr(dry_run, "OUT", str(out))
    runs = _runs_file(pipeline, [_failed(104), {**_failed(103), "conclusion": "success"}])
    assert dry_run.main(["--with-failed-runs", str(runs)]) == 0
    html = out.read_text()
    assert emailer.ALERT_ONLY_NOTE in html and " failed: Anthropic API access failed." in html
    assert html.count("Postings from that run were not scored.") == 1          # the success is not listed
    assert "[DryRun] subject: [DRY RUN] Job digest: fetch runs failed" in capsys.readouterr().out
    assert not (pipeline / state.SKIP_STATE_FILE).exists()                      # no state written


def test_dry_run_with_failed_runs_and_postings_renders_the_banner(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(llm, "_provider", FakeProvider(reply=GOOD_SCORE))
    main.fetch_run()
    out = pipeline / "out" / "digest_dry_run.html"
    monkeypatch.setattr(dry_run, "OUT", str(out))
    assert dry_run.main(["--with-failed-runs", str(_runs_file(pipeline, [_failed(104)]))]) == 0
    html = out.read_text()
    assert " failed: Anthropic API access failed." in html and emailer.ALERT_ONLY_NOTE not in html
    # Both sample postings name the same hiring company: one card (FIL-13).
    assert "[DryRun] subject: [DRY RUN] Job digest — 1 match ·" in capsys.readouterr().out


def test_dry_run_without_the_flag_has_no_banner(pipeline, monkeypatch):
    out = pipeline / "out" / "digest_dry_run.html"
    monkeypatch.setattr(dry_run, "OUT", str(out))
    assert dry_run.main([]) == 0
    assert "Postings from that run were not scored" not in out.read_text()


def test_dry_run_with_a_missing_runs_file_fails(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(dry_run, "OUT", str(pipeline / "out.html"))
    assert dry_run.main(["--with-failed-runs", str(pipeline / "nope.json")]) == 1
    assert "no such file" in capsys.readouterr().out
