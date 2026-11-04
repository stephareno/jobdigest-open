"""FIL-10: the same opening from two sources, or from one source twice, is one row.

The second dedup key (normalised company + title + location) is checked after
the URL key and before scoring, against earlier fetch runs as well as this one.
Two postings on the same board under different ids are never merged.

Covers: an Adzuna ad re-served daily under a rotating tracking token costs one
row and one scoring call ever; a Dragos copy arriving a week later costs
neither; MITRE's two requisitions stay two rows; the surviving copy is the
highest-priority source's, with the highest score's rationale; an old-format
seen_jobs.json still loads and no URL hash moves. No network access.
"""

import importlib
import json
import os
import re
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

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

import main  # noqa: E402
import pending  # noqa: E402
import state  # noqa: E402
from sources import startups  # noqa: E402

# Real links from the Sheet backtest. Titles are the Sheet's own except where
# noted: a few of the backtest's worst repeaters carry titles the current gate
# drops, so those cases keep the real URLs and take a gate-passing title.
ELASTIC = "https://www.adzuna.com/land/ad/5873276779?se={}&v=0BD1291E74AFE62897635C6BA39D0A6914EC5EC6"
DRAGOS_ATS = "https://job-boards.greenhouse.io/dragos/jobs/5399442008"
DRAGOS_LINKEDIN = "https://www.linkedin.com/jobs/view/4457489553"
DRAGOS_ADZUNA = "https://www.adzuna.com/details/5866632840"
MITRE_A = "https://careers.mitre.org/us/en/job/R117090/Lead-Systems-Engineer"
MITRE_B = "https://careers.mitre.org/us/en/job/R116664/Lead-Systems-Engineer"


def _job(url, title, company, source, location="Remote, US", score=8, **extra):
    return {"title": title, "company": company, "apply_link": url, "source": source,
            "location": location, "work_model": "remote", "description": "d",
            "score": score, **extra}


def _dragos(url, source, title="Vice President, Information Technology",
            location="Hanover, MD", score=9):
    return _job(url, title, "Dragos", source, location=location, score=score)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """main.py with every network edge replaced; records scoring calls and rows."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path / "state" / "pending")
    (tmp_path / state.STATE_FILE).write_text(
        json.dumps({"seed": datetime.utcnow().isoformat()}))
    main.SOURCE_OK.clear()

    calls = {"scored": [], "sheet": [], "disq": [], "emails": []}
    catalog = {}

    def source(name):
        def fetch():
            return [dict(j) for j in catalog.get(name, [])]
        return fetch

    monkeypatch.setattr(main, "SOURCES", [(n, source(n)) for n, _ in main.SOURCES])
    monkeypatch.setattr(main, "fetch_startups",
                        lambda slot=None: startups.FetchResult(
                            [dict(j) for j in catalog.get("Startup", [])], complete=True))
    monkeypatch.setattr(main, "load_connections", lambda: {})
    monkeypatch.setattr(main, "find_warm_contact", lambda company, connections: "")
    monkeypatch.setattr(main, "resolve_jobs", lambda jobs: None)
    monkeypatch.setattr(main, "get_declined_feedback", lambda: [])

    def fake_score(jobs, declined_feedback=None):
        calls["scored"].extend(f"{j['company']}|{j['title']}" for j in jobs)
        return [{**j, "rationale": f"rationale from {j['source']}",
                 "score": j.get("score", 8)} for j in jobs]

    monkeypatch.setattr(main, "score_jobs", fake_score)
    monkeypatch.setattr(main, "append_jobs", lambda jobs: calls["sheet"].extend(jobs))
    monkeypatch.setattr(main, "append_disqualified_jobs", lambda jobs: calls["disq"].extend(jobs))
    monkeypatch.setattr(main, "send_digest", lambda sections, **kw: calls["emails"].append(sections))
    monkeypatch.setattr(main, "get_unseen_jobs", lambda: [])
    monkeypatch.setitem(sys.modules, "sources.applied_doc",
                        types.SimpleNamespace(seed_from_doc=lambda: None))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: None))
    return calls, catalog


# ------------------------------------------------------- across fetch runs

def test_adzuna_ad_reserved_daily_costs_one_row_and_one_scoring_call(pipeline):
    """Elastic ad 5873276779 took 11 Sheet rows: the `se=` token rotates every
    day, so the URL key never recognises it. The content key does."""
    calls, catalog = pipeline
    for day, token in enumerate(("6NwF-O6q8RGyxtY0rUukWA", "SuR82rKr8RGzD_gtUNDNoQ",
                                 "TLY88Hus8RGCZNt6PdeKCQ")):
        catalog["adzuna"] = [_job(ELASTIC.format(token), "Director - IT Software Engineering",
                                  "Elastic", "Adzuna",
                                  location="Washington, Washington, D.C.")]
        main.fetch_run(shard=None)
        if day == 0:
            assert len(calls["scored"]) == 1
    assert len(calls["scored"]) == 1, "only the first sighting should reach the scorer"
    main.digest_run()
    assert len(calls["sheet"]) == 1


def test_a_copy_arriving_a_week_later_is_not_scored_again(pipeline):
    """Dragos: LinkedIn first, the Greenhouse copy days later, Adzuna later still."""
    calls, catalog = pipeline
    catalog["linkedin_email"] = [_dragos(DRAGOS_LINKEDIN, "LinkedIn")]
    main.fetch_run(shard=None)
    main.digest_run()

    catalog["linkedin_email"] = []
    catalog["ats"] = [_dragos(DRAGOS_ATS, "ATS")]
    catalog["adzuna"] = [_dragos(DRAGOS_ADZUNA, "Adzuna",
                                 location="Hanover, Anne Arundel County")]
    main.fetch_run(shard=None)
    main.digest_run()

    assert calls["scored"] == ["Dragos|Vice President, Information Technology"]
    assert len(calls["sheet"]) == 1
    assert len(calls["emails"]) == 1


def test_two_requisitions_on_one_board_stay_two_rows(pipeline):
    """MITRE R117090 and R116664 share company, title and location. The real
    pair is titled "Lead Systems Engineer", which the title gate drops, so the
    real URLs carry a gate-passing title here."""
    calls, catalog = pipeline
    catalog["jsearch"] = [_job(MITRE_A, "Director, Information Technology",
                               "The MITRE Corporation", "JSearch", location="McLean, Virginia")]
    main.fetch_run(shard=None)
    catalog["jsearch"] = [_job(MITRE_B, "Director of IT",
                               "The Mitre Corporation", "JSearch", location="McLean, Virginia")]
    main.fetch_run(shard=None)
    main.digest_run()
    assert len(calls["scored"]) == 2
    assert sorted(j["apply_link"] for j in calls["sheet"]) == sorted([MITRE_A, MITRE_B])


def test_a_queued_posting_is_not_refetched_before_its_digest(pipeline):
    """Two fetch runs between digests: the second must not queue a second copy."""
    calls, catalog = pipeline
    catalog["linkedin_email"] = [_dragos(DRAGOS_LINKEDIN, "LinkedIn")]
    main.fetch_run(shard=None)
    catalog["linkedin_email"] = []
    catalog["ats"] = [_dragos(DRAGOS_ATS, "ATS")]
    main.fetch_run(shard=None)
    main.digest_run()
    assert len(calls["scored"]) == 1
    assert len(calls["sheet"]) == 1


# ------------------------------------------------------- within one run

def test_the_highest_priority_source_supplies_the_apply_link(pipeline):
    """Adzuna is fetched first but ranks last (rule 6), so the ATS link wins."""
    calls, catalog = pipeline
    catalog["adzuna"] = [_dragos(DRAGOS_ADZUNA, "Adzuna",
                                 location="Hanover, Anne Arundel County")]
    catalog["ats"] = [_dragos(DRAGOS_ATS, "ATS")]
    catalog["linkedin_email"] = [_dragos(DRAGOS_LINKEDIN, "LinkedIn")]
    main.fetch_run(shard=None)
    main.digest_run()
    assert len(calls["scored"]) == 1
    assert [j["apply_link"] for j in calls["sheet"]] == [DRAGOS_ATS]
    assert calls["sheet"][0]["source"] == "ats"


# ------------------------------------------------------- pending.dedupe

def test_dedupe_keeps_the_best_link_and_the_highest_score(pipeline):
    """Copies queued by different fetch runs meet in the digest, already scored."""
    _calls, _catalog = pipeline
    queued = [
        {**_dragos(DRAGOS_ADZUNA, "Adzuna", location="Hanover, Anne Arundel County",
                   score=6), "rationale": "adzuna read", "found_at": "2026-09-02"},
        {**_dragos(DRAGOS_ATS, "ATS", score=9),
         "rationale": "ats read the JD", "found_at": "2026-09-03"},
        {**_dragos(DRAGOS_LINKEDIN, "LinkedIn", score=7),
         "rationale": "linkedin read", "found_at": "2026-09-04"},
    ]
    out = pending.dedupe(queued)
    assert len(out) == 1
    assert out[0]["apply_link"] == DRAGOS_ATS
    assert out[0]["score"] == 9
    assert out[0]["rationale"] == "ats read the JD"
    assert sorted(out[0]["merged_from"]) == ["Adzuna", "LinkedIn"]


def test_dedupe_adopts_a_losing_copys_higher_score_and_its_rationale(pipeline):
    _calls, _catalog = pipeline
    queued = [
        {**_dragos(DRAGOS_ATS, "ATS", score=6), "rationale": "ats thin read",
         "found_at": "2026-09-03"},
        {**_dragos(DRAGOS_LINKEDIN, "LinkedIn", score=9), "rationale": "full JD",
         "found_at": "2026-09-04"},
    ]
    out = pending.dedupe(queued)
    assert len(out) == 1
    assert out[0]["apply_link"] == DRAGOS_ATS   # link still from the better source
    assert (out[0]["score"], out[0]["rationale"]) == (9, "full JD")


def test_dedupe_hands_over_every_field_the_winning_score_was_built_from(pipeline):
    """A survivor must never carry one copy's score with another copy's reason
    for it, so everything apply_location_tier writes travels with the score —
    including GAP-07's dc_bonus."""
    _calls, _catalog = pipeline
    low = {**_dragos(DRAGOS_ATS, "ATS", location="Reston, VA", score=5),
           "work_model": "Hybrid", "rationale": "ats thin read", "base_score": 5,
           "location_penalty": 0, "location_method": "posting",
           "location_reason": "commutable (Washington DC, 18 mi)", "dc_bonus": 0,
           "relo": False, "found_at": "2026-09-03"}
    high = {**_dragos(DRAGOS_LINKEDIN, "LinkedIn", location="Reston, VA", score=9),
            "work_model": "Hybrid", "rationale": "full JD, DC bonus applied",
            "base_score": 8, "location_penalty": 0, "location_method": "posting",
            "location_reason": "commutable (Washington DC, 18 mi), DC bonus +1",
            "dc_bonus": 1, "relo": False, "found_at": "2026-09-04"}
    out = pending.dedupe([low, high])
    assert len(out) == 1
    survivor = out[0]
    assert survivor["apply_link"] == DRAGOS_ATS
    for field in ("score", "rationale", "base_score", "location_reason", "dc_bonus"):
        assert survivor[field] == high[field], field


def test_every_field_apply_location_tier_writes_is_handed_over(pipeline):
    """Guards the list itself: a new field added to apply_location_tier has to
    be added to pending._SCORE_FIELDS too, or it silently stops travelling."""
    import inspect

    import location
    written = set(re.findall(r'job\["(\w+)"\]\s*=',
                             inspect.getsource(location.apply_location_tier)))
    assert written <= set(pending._SCORE_FIELDS), (
        f"apply_location_tier writes {sorted(written - set(pending._SCORE_FIELDS))}, "
        "which pending._SCORE_FIELDS does not hand to the survivor")


def test_dedupe_keeps_two_requisitions_on_one_board(pipeline):
    _calls, _catalog = pipeline
    queued = [
        {**_job(MITRE_A, "Director, Information Technology", "MITRE", "JSearch",
                location="McLean, Virginia"), "found_at": "2026-06-24"},
        {**_job(MITRE_B, "Director of IT", "MITRE", "JSearch",
                location="McLean, Virginia"), "found_at": "2026-09-02"},
    ]
    assert len(pending.dedupe(queued)) == 2


# ------------------------------------------------------- seen_jobs.json shape

def test_old_format_state_still_loads_and_url_hashes_never_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = {state.job_hash(DRAGOS_ATS): "2026-09-01T00:00:00",
           state.job_hash(DRAGOS_LINKEDIN): "2026-09-02T00:00:00"}
    (tmp_path / state.STATE_FILE).write_text(json.dumps(old))

    assert state.is_seen(DRAGOS_ATS)
    assert state.load_content_index() == {}
    assert state.last_run_date() == datetime(2026, 9, 2)

    key = state.content_key({"company": "Dragos", "title": "VP, IT", "location": "Hanover, MD"})
    state.mark_content_seen_batch([(key, [DRAGOS_ATS])])

    after = json.loads((tmp_path / state.STATE_FILE).read_text())
    for url_hash, ts in old.items():
        assert after[url_hash] == ts
    assert state.load_content_index()[state.content_hash(key)] == [
        ("job-boards.greenhouse.io", "5399442008")]


def test_purge_ages_out_both_kinds_of_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = (datetime.utcnow() - timedelta(days=state.RETENTION_DAYS + 5)).isoformat()
    fresh = datetime.utcnow().isoformat()
    (tmp_path / state.STATE_FILE).write_text(json.dumps({
        "aaa": old, "bbb": fresh,
        "c:old": {"ts": old, "boards": []},
        "c:new": {"ts": fresh, "boards": ["linkedin.com|1"]},
    }))
    assert state.purge_old_entries() == 2
    remaining = json.loads((tmp_path / state.STATE_FILE).read_text())
    assert sorted(remaining) == ["bbb", "c:new"]


def test_both_the_aggregator_and_the_resolved_link_are_recorded(tmp_path, monkeypatch):
    """A JSearch link resolving to the employer's board must leave a guard
    ident on both hosts, or the guard cannot fire on either next time."""
    monkeypatch.chdir(tmp_path)
    key = state.content_key({"company": "MITRE", "title": "Director, Information Technology",
                             "location": "McLean, VA"})
    state.mark_content_seen_batch([(key, ["https://www.linkedin.com/jobs/view/4400411552",
                                          MITRE_A])])
    idents = state.load_content_index()[state.content_hash(key)]
    assert sorted(idents) == [("careers.mitre.org", "r117090"),
                              ("www.linkedin.com", "4400411552")]
