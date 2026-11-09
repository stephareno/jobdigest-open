"""REV-08: LinkedIn page fetching is an explicit opt-in, and every doc says what
the code does.

Covers: with sources.linkedin_email.fetch_pages false the source builds
title-only postings from the alert email and never requests linkedin.com; the
scoring call carries the "Description unavailable" line; function fit is
"unknown" and not capped; the "title only" tag renders and the Sheet's
Function Fit says "unknown"; true keeps REV-07's page handling; the switch
validates as a bool and defaults to false; the interview asks and records it;
no shipped document says "never touches" and the README, page, PRD and setup
guide carry the shipped sentence. No network access.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import state, workdir  # noqa: E402,F401
from test_rev07_linkedin import PAGES, _url  # noqa: E402

import emailer  # noqa: E402
import location  # noqa: E402
import profile_loader  # noqa: E402
import scorer  # noqa: E402
import setup_interview  # noqa: E402
import sheets  # noqa: E402
import sources.linkedin_email as li  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SENTENCE = ("Reads the job-alert emails LinkedIn sends you from one Gmail label. Opening each job's public page to "
            "read the description is a separate switch, off by default: LinkedIn's user agreement prohibits "
            "automated access to its site and it blocks many such requests, so turn it on only if you accept that; "
            "off, jobs are scored from the alert alone and marked 'title only'.")
PRIVACY = "if page fetching is on, LinkedIn receives requests for each job page from the runner's address"
def _shipped(name):
    """docs/public/<name> in v2; the export ships it at the repository root."""
    staged = ROOT / "docs" / "public" / name
    return staged if staged.exists() else ROOT / name


DOCS = {"README": _shipped("README.md"), "PRD": ROOT / "docs" / "PRD.md",
        "SETUP_GUIDE": ROOT / "docs" / "SETUP_GUIDE.md", "index.html": ROOT / "docs" / "index.html"}
ALERTS = {"4242": {"title": "Director of IT", "company": "Initech", "location": "Austin, TX (Hybrid)"},
          "4343": {"title": "VP, Technology", "company": "Globex", "location": "Remote"},
          "4444": {}}


def _switch(monkeypatch, on):
    real = profile_loader.active()
    sources = dict(real["sources"], linkedin_email={"fetch_pages": on})
    monkeypatch.setattr(profile_loader, "active", lambda: {**real, "sources": sources})


@pytest.fixture
def source(workdir, monkeypatch):
    requested = []

    def get(url, **kw):
        requested.append(url)
        return PAGES[url.rstrip("/").rsplit("/", 1)[-1]]()
    monkeypatch.setattr(li.requests, "get", get)
    monkeypatch.setattr(li.time, "sleep", lambda s: None)

    def run(ids):
        monkeypatch.setattr(li, "_collect_job_ids_from_email",
                            lambda: (li.ALERT_INFO.update({k: v for k, v in ALERTS.items() if v}), list(ids))[1])
        return li.fetch_all()
    return run, requested


# ---------------------------------------------------------- fetch_pages false

def test_off_builds_title_only_postings_from_the_alert_and_never_requests_linkedin(source, monkeypatch, capsys):
    run, requested = source
    _switch(monkeypatch, False)
    jobs = run(["4242", "4343", "4444"])
    assert requested == []                                                    # linkedin.com never asked
    assert [(j["title"], j["company"], j["location"]) for j in jobs] == [
        ("Director of IT", "Initech", "Austin, TX (Hybrid)"), ("VP, Technology", "Globex", "Remote")]
    assert all(j["title_only"] and j["description"] == "" and j["source"] == "linkedin_email" for j in jobs)
    assert jobs[0]["apply_link"] == _url("4242") and jobs[0]["work_model"] == "Hybrid"
    assert jobs[1]["work_model"] == "Remote"
    assert jobs.counts == {"title_only": 2, "no_alert_details": 1}
    assert jobs.summary == ("alert emails only (sources.linkedin_email.fetch_pages false): 2 title-only postings, "
                            "1 jobs skipped (the alert gave no title)")
    assert "4444: the alert email gave no title; skipped" in capsys.readouterr().out
    jobs.commit()                                                             # no page failures to save
    assert state.load_linkedin_failures() == {}


def test_the_scoring_call_carries_the_line_and_fit_is_unknown(llm_stub):
    reply = {"score": 7, "rationale": {"company": "c", "role": "r", "culture": "no signal in posting."},
             "criteria_hits": [], "hard_block_industry": None, "is_cool_company_wrong_role": False,
             "function_fit": "outside_department", "work_model": "Hybrid", "city": None, "state": None,
             "hiring_company": "Initech", "website": None}
    stub = llm_stub([json.dumps(reply), json.dumps(reply)])
    job = li.alert_only_job("4242", ALERTS["4242"])
    result = scorer._score_single(job)
    assert ('Description unavailable: judge from the title and company only. function_fit may be "unknown".'
            in stub.calls[0]["content"])
    assert result["function_fit"] == "unknown"
    normal = scorer._score_single({"title": "Director of IT", "company": "Acme", "description": "Leads IT."})
    assert "Description unavailable" not in stub.calls[1]["content"]
    assert normal["function_fit"] == "outside_department"


def _tier(job):
    job = {"location": "Remote, US", "work_model": "Remote", "comp": "", "rationale": "Blurb.", **job}
    job.setdefault("location_penalty", 0)
    location.apply_location_tier(job, base_score=8)
    return job


def test_no_function_cap_on_a_title_only_unknown():
    capped = _tier({"function_fit": "unknown"})
    assert capped["function_capped"] and capped["score"] <= location.CONFIG.function_cap
    title_only = _tier({"function_fit": "unknown", "title_only": True})
    assert not title_only["function_capped"] and title_only["score"] > location.CONFIG.function_cap
    still = _tier({"function_fit": "outside_department", "title_only": True})   # only "unknown" is exempt
    assert still["function_capped"]


def test_the_title_only_tag_renders_and_the_sheet_says_unknown():
    job = {**li.alert_only_job("4242", ALERTS["4242"]), "score": 7, "rationale": "Blurb.",
           "function_fit": "unknown"}
    html = emailer._job_card(job)
    assert ">7/10</span><span" in html and ">title only</span>" in html
    assert "title only" not in emailer._job_card({**job, "title_only": False})
    assert sheets.main_row_data(job)["Function Fit"] == "unknown"


# ----------------------------------------------------------- fetch_pages true

def test_on_keeps_the_rev07_page_handling(source, monkeypatch):
    run, requested = source
    _switch(monkeypatch, True)
    jobs = run(["100", "999"])
    assert requested == [_url("100"), _url("999")]
    assert [j["title"] for j in jobs] == ["Director of IT"] and not jobs[0].get("title_only")
    assert jobs.counts["blocked"] == 1 and jobs.counts["fetched"] == 1
    jobs.commit()
    assert state.load_linkedin_failures() == {state.linkedin_fail_key(_url("999")): 1}


# ---------------------------------------------------------------- profile

def _profile(**li_section):
    data = yaml.safe_load((ROOT / "profile.example.yaml").read_text())
    if li_section.get("drop"):
        data["sources"].pop("linkedin_email", None)
    else:
        data["sources"]["linkedin_email"] = li_section
    return data


def test_the_switch_is_a_bool_and_defaults_to_false(tmp_path):
    assert any("fetch_pages" in e for e in profile_loader.validate(_profile(fetch_pages="yes")))
    assert profile_loader.validate(_profile(fetch_pages=True)) == []
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(_profile(drop=True)))
    assert profile_loader.load(path)["sources"]["linkedin_email"]["fetch_pages"] is False
    assert profile_loader.load(ROOT / "profile.example.yaml")["sources"]["linkedin_email"]["fetch_pages"] is False
    if (ROOT / "profile.yaml").exists():                                      # not in the public export
        assert profile_loader.load(ROOT / "profile.yaml")["sources"]["linkedin_email"]["fetch_pages"] is True


def test_the_example_profile_explains_the_switch():
    text = (ROOT / "profile.example.yaml").read_text()
    for part in ("true: the agent opens each job's public LinkedIn page", "without logging in",
                 "LinkedIn's user agreement prohibits automated access to its site",
                 "false: jobs are scored from the alert email", "tagged 'title only' in the digest"):
        assert part in text


def test_the_interview_asks_and_records_it(tmp_path):
    (tmp_path / "data").mkdir()
    asked, said = [], []

    def ask(prompt):
        asked.append(prompt)
        return "y" if "public page to read the description" in prompt else ""
    assert setup_interview.main([], ask=ask, say=said.append, profile_path=tmp_path / "profile.yaml",
                                root=tmp_path, run_after=False) == 0
    data = yaml.safe_load((tmp_path / "profile.yaml").read_text())
    assert data["sources"]["linkedin_email"]["fetch_pages"] is True
    assert any("public page to read the description [n]" in p for p in asked)  # default no
    assert any("user agreement prohibits automated access" in " ".join(str(s).split()) for s in said)


# ------------------------------------------------------------------ docs

def test_no_document_says_never_touches():
    for name, path in DOCS.items():
        assert "never touches" not in path.read_text().lower(), name
    agents = _shipped("AGENTS.md")
    assert "never touches" not in agents.read_text().lower()


@pytest.mark.parametrize("name", list(DOCS))
def test_each_document_carries_the_shipped_sentence(name):
    import html
    text = DOCS[name].read_text()
    if name == "index.html":
        text = html.unescape(text)
    assert SENTENCE in text, name


@pytest.mark.parametrize("name", ["README", "index.html", "SETUP_GUIDE", "PRD"])
def test_each_privacy_section_names_the_page_requests(name):
    import html
    assert PRIVACY in html.unescape(DOCS[name].read_text()), name
