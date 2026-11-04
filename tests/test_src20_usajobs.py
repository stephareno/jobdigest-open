"""SRC-20 part B: the USAJOBS source.

tests/fixtures/usajobs_search_schema_sample.json is NOT a live capture: it was
written from the documented search-API response schema (developer.usajobs.gov
and its published swagger) because USAJOBS_API_KEY was not yet available.
Replace it with a real response once the key is set:
    python3 -m sources.usajobs --save-fixture tests/fixtures/usajobs_search.json
"""

import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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

import config  # noqa: E402
import location  # noqa: E402
import main  # noqa: E402
from sources import usajobs  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "usajobs_search_schema_sample.json"


@pytest.fixture
def data():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def items(data):
    return data["SearchResult"]["SearchResultItems"]


def by_title(jobs, title):
    return next(j for j in jobs if j["title"] == title)


def test_pay_floor_reads_the_top_of_the_annual_range(items):
    jobs, counts = usajobs.select(items, min_comp=200000)
    assert {k: counts[k] for k in ("fetched", "below_floor", "no_annual_pay", "kept")} == \
        {"fetched": 5, "below_floor": 2, "no_annual_pay": 1, "kept": 2}
    assert {j["title"] for j in jobs} == {"Deputy Chief Information Officer",
                                          "Chief Information Officer"}


def test_gs15_top_passes_a_180k_floor(items):
    # DISA GS-15 tops out at $195,200: below $200K, above $180K (SCO-08).
    jobs, counts = usajobs.select(items, min_comp=180000)
    assert "SUPV IT SPECIALIST (PLCYPLN)" in {j["title"] for j in jobs}
    assert counts["kept"] == 3


def test_floor_defaults_to_config(items, monkeypatch):
    monkeypatch.setattr(usajobs, "PAY_FLOOR", 190000)
    _, counts = usajobs.select(items)
    assert counts["kept"] == 3


def test_mapping(items):
    jobs, _ = usajobs.select(items, min_comp=0)
    nasa = by_title(jobs, "Deputy Chief Information Officer")
    assert nasa["company"] == "National Aeronautics and Space Administration"
    assert nasa["apply_link"] == "https://www.usajobs.gov/job/845100100"
    assert nasa["source"] == "usajobs"
    assert nasa["comp"] == "$150,160 – $225,700"
    assert nasa["date_posted"].startswith("2026-10-01")
    assert nasa["close_date"].startswith("2026-10-20")
    assert "Serves as Deputy Chief Information Officer" in nasa["description"]
    assert "Directs enterprise infrastructure" in nasa["description"]
    # OrganizationName, not the department: matches LinkedIn's "Defense Information Systems Agency"
    disa = by_title(jobs, "SUPV IT SPECIALIST (PLCYPLN)")
    assert disa["company"] == "Defense Information Systems Agency"


def test_work_model(items):
    jobs, _ = usajobs.select(items, min_comp=0)
    assert by_title(jobs, "Deputy Chief Information Officer")["work_model"] == "Hybrid"  # telework only
    assert by_title(jobs, "SUPV IT SPECIALIST (PLCYPLN)")["work_model"] == "In-office"
    assert by_title(jobs, "IT Specialist (INFOSEC)")["work_model"] == "Remote"  # "Anywhere in the U.S."
    assert by_title(jobs, "Chief Information Officer")["work_model"] == "Remote"  # RemoteIndicator


def test_every_location_kept_and_most_favourable_tier_counts(items):
    item = json.loads(json.dumps(items[4]))
    item["MatchedObjectDescriptor"]["UserArea"]["Details"]["RemoteIndicator"] = False
    job = usajobs.to_job(item)
    assert job["location"] == "Washington, District of Columbia; Denver, Colorado"
    job["location"] = "Chicago, Illinois; Denver, Colorado"
    tier, method, bonus = location.location_resolution(job)
    assert tier.penalty == 0 and method == "posting" and bonus == location.CONFIG.home_bonus


def test_paging_stops_at_number_of_pages(data, monkeypatch):
    pages = []

    def fake_get(session, headers, params, page):
        pages.append(page)
        out = json.loads(json.dumps(data))
        out["SearchResult"]["UserArea"]["NumberOfPages"] = "3"
        return out

    monkeypatch.setattr(usajobs, "_get_page", fake_get)
    monkeypatch.setattr(usajobs, "PAGE_DELAY_SECS", 0)
    got = usajobs.fetch_pass(None, {}, "test", {})
    assert pages == [1, 2, 3]
    assert len(got) == 3 * len(data["SearchResult"]["SearchResultItems"])


def test_search_passes():
    passes = dict(usajobs.search_passes())
    series = passes["series 2210"]
    assert series["JobCategoryCode"] == "2210"
    assert series["HiringPath"] == "public;ses"
    assert series["DatePosted"] == 7 and series["ResultsPerPage"] == 500
    keywords = {p["Keyword"] for p in passes.values() if "Keyword" in p}
    assert "Chief Information Officer" in keywords
    assert not any("Security" in k for k in keywords)
    assert not any("LocationName" in p for p in passes.values())


def test_missing_key_fails_loudly(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="USAJOBS_API_KEY"):
        usajobs.fetch_all()


def test_missing_key_never_blocks_the_fetch(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    import time
    assert main.run_source("USAJOBS", usajobs.fetch_all, time.monotonic() + 60) == []
    assert main.SOURCE_OK["USAJOBS"] is False


def test_wiring():
    assert ("usajobs", usajobs.fetch_all) in [(n, f) for n, f in main.SOURCES]
    assert "usajobs" in config.DAILY_SOURCES
    assert "usajobs" in config.SOURCE_BUDGET_SECS
    # Not gated: the gate would drop DISA's score-7 Supervisory IT Specialist.
    assert "usajobs" not in config.TITLE_GATED_SOURCES
    assert not config.title_gate("SUPV IT SPECIALIST (PLCYPLN)")


def test_office_names_fall_back_to_the_department():
    # As the first live run (6 Oct) named them.
    cases = {
        ("Headquarters, NASA", "National Aeronautics and Space Administration"):
            "National Aeronautics and Space Administration",
        ("Offices, Boards and Divisions", "Department of Justice"): "Department of Justice",
        ("Deputy Assistant Secretary for Information and Technology",
         "Department of Veterans Affairs"): "Department of Veterans Affairs",
        ("Defense Information Systems Agency", "Department of Defense"):
            "Defense Information Systems Agency",
        ("Office of Personnel Management", "Office of Personnel Management"):
            "Office of Personnel Management",
        ("Veterans Health Administration", "Department of Veterans Affairs"):
            "Veterans Health Administration",
    }
    for (org, dept), want in cases.items():
        assert usajobs.agency({"OrganizationName": org, "DepartmentName": dept}) == want


def test_keyword_hits_need_the_title_in_the_job_title(data, monkeypatch):
    # The first live run drew 118 VA physician postings through full-text
    # "Chief Information Officer"; only the series pass may keep an off-title hit.
    physician = json.loads(json.dumps(data["SearchResult"]["SearchResultItems"][0]))
    physician["MatchedObjectId"] = "845199900"
    physician["MatchedObjectDescriptor"]["PositionTitle"] = "Physician - Primary Care"
    physician["MatchedObjectDescriptor"]["OrganizationName"] = "Veterans Health Administration"

    def fake_pass(session, headers, label, params):
        if "Keyword" in params:
            return [physician] + data["SearchResult"]["SearchResultItems"]
        return []

    monkeypatch.setenv("USAJOBS_API_KEY", "test")
    monkeypatch.setattr(usajobs, "fetch_pass", fake_pass)
    titles = {j["title"] for j in usajobs.fetch_all()}
    assert "Physician - Primary Care" not in titles
    assert "Deputy Chief Information Officer" in titles


def test_below_floor_counted_by_grade(items):
    _, counts = usajobs.select(items, min_comp=200000)
    assert counts["below_floor_by_grade"] == {"GS-15": 1, "GS-13": 1}
