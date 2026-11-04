"""SCO-09: the scoring call classifies the function a role leads, and any value
but leads_department caps the final score at scoring.function_cap (4), in code,
after every other adjustment.

Covers: the cap after the location tier, DC bonus and comp adjustment; a 0
stays 0 and the floor of 1 holds; jobs scored before SCO-09 (no function_fit)
are never capped; a missing or invalid reply value is capped; the rationale
names the cap once across re-tiers; capped rows never reach main or
worth_a_look, dream companies included, but do reach "Cool company, wrong
role"; the fields travel through pending.dedupe; the Sheet writes "Function
Fit" only into an existing column; the prompt defines both fields. No network.
"""

import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_SHEET_ID", "x")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
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
import emailer  # noqa: E402
import pending  # noqa: E402
import profile_loader  # noqa: E402
import llm  # noqa: E402
import scorer  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402
import sheets  # noqa: E402
from location import apply_location_tier  # noqa: E402


def _job(fit="leads_department", description="", location="Remote, US", work_model="Remote", **kw):
    job = {"title": "Director of IT", "company": "Acme", "location": location,
           "work_model": work_model, "description": description, "comp": "",
           "rationale": "Strong fit; mission read.", **kw}
    if fit is not None:
        job["function_fit"] = fit
    return job


def test_leadership_is_never_capped():
    job = _job()
    assert apply_location_tier(job, base_score=9, record=False) == 9
    assert job["function_capped"] is False
    assert "capped" not in job["rationale"]


@pytest.mark.parametrize("fit", ["in_department", "outside_department", "unknown"])
def test_anything_else_caps_at_four(fit):
    job = _job(fit)
    assert apply_location_tier(job, base_score=9, record=False) == profile_loader.active()["scoring"]["function_cap"] == 4
    assert job["function_capped"] is True
    assert job["base_score"] == 9


def test_cap_comes_after_every_other_adjustment():
    # 3 + home bonus +1 + comp +1 = 5, then capped to 4
    dc = _job("outside_department", description="Pay range: $250,000 - $300,000",
              location="Chicago, IL", work_model="Hybrid")
    assert apply_location_tier(dc, base_score=3, record=False) == 4
    assert dc["home_bonus"] == 1 and dc["comp_adjustment"] == 1
    # 9 - 5 relo = 4: already at the cap, still marked capped
    relo = _job("in_department", location="Jacksonville, FL", work_model="In-office")
    assert apply_location_tier(relo, base_score=9, record=False) == 4
    # below the cap, the other adjustments decide; floor of 1 holds
    low = _job("in_department", description="Pay range: $80,000 - $97,000 annually.")
    assert apply_location_tier(low, base_score=2, record=False) == 1


def test_zero_stays_zero_and_is_not_marked_capped():
    job = _job("outside_department")
    assert apply_location_tier(job, base_score=0, record=False) == 0
    assert job["function_capped"] is False


def test_pre_sco09_jobs_are_not_capped():
    job = _job(fit=None)
    assert apply_location_tier(job, base_score=9, record=False) == 9
    assert job["function_capped"] is False


def test_rationale_names_the_cap_once():
    job = _job("in_department", description="Pay range: $80,000 - $97,000 annually.")
    apply_location_tier(job, base_score=8, record=False)
    apply_location_tier(job, base_score=8, record=False)
    assert job["rationale"].count("capped at 4") == 1
    assert job["rationale"].count("Comp tops out") == 1
    assert "in the target department but does not lead it" in job["rationale"]
    leader = {**job, "function_fit": "leads_department"}
    apply_location_tier(leader, base_score=8, record=False)
    assert "capped" not in leader["rationale"]


def _filtered(**kw):
    job = _job(**kw)
    apply_location_tier(job, base_score=kw.pop("base", 9), record=False)
    return job, scorer.filter_jobs([job])


def test_capped_rows_never_reach_main_or_worth_a_look_even_for_dream_companies():
    job, sections = _filtered(fit="outside_department", is_dream_company=True)
    assert job["score"] == 4
    assert sections["main"] == [] and sections["worth_a_look"] == []


def test_capped_cool_company_rows_go_to_cool_wrong_role():
    job, sections = _filtered(fit="outside_department", is_cool_company_wrong_role=True)
    assert sections["cool_wrong_role"] == [job]


def test_uncapped_dream_shortcut_still_works():
    job = _job(is_dream_company=True)
    apply_location_tier(job, base_score=3, record=False)
    assert scorer.filter_jobs([job])["main"] == [job]


def test_cool_card_names_the_cap():
    job = _job("outside_department", is_cool_company_wrong_role=True, apply_link="https://x/1")
    apply_location_tier(job, base_score=9, record=False)
    html = emailer._build_html([], [], [job], [], False, False)
    assert "Function: outside the target department: capped at 4." in html


def test_function_fields_travel_with_the_score_in_dedupe():
    assert "function_fit" in pending._SCORE_FIELDS
    assert "function_capped" in pending._SCORE_FIELDS


def test_sheet_writes_function_fit_only_into_an_existing_column():
    data = sheets.main_row_data({**_job("in_department"), "apply_link": "https://x/1"})
    assert data["Function Fit"] == "in_department"
    assert sheets._row_values(data, ["Job Title", "Score"]) == ["Director of IT", ""]
    src = Path(sheets.__file__).read_text()
    assert '_ensure_header(sheet, headers, "Function Fit")' not in src


@pytest.mark.parametrize("reply, stored", [
    ("leads_department", "leads_department"),
    (" Not_IT ", "outside_department"),
    (None, "unknown"),
    ("manager", "unknown"),
])
def test_reply_value_is_normalised(monkeypatch, reply, stored):
    payload = {"score": 7, "rationale": "r", "is_insurance": False,
               "is_cool_company_wrong_role": False, "work_model": "Remote",
               "city": None, "state": None, "hiring_company": "Acme"}
    if reply is not None:
        payload["function_fit"] = reply
    monkeypatch.setattr(llm, "_provider", StubProvider(lambda call: json.dumps(payload)))
    assert scorer._score_single({"title": "t", "company": "c"})["function_fit"] == stored


def test_prompt_defines_function_fit_and_cool_company_wrong_role():
    prompt = scorer.SYSTEM_PROMPT
    assert '"function_fit": "<leads_department|in_department|outside_department>"' in prompt
    for value in config.FUNCTION_FIT_VALUES:
        assert f'"{value}"' in prompt
    assert "Supervisory IT Specialist" in prompt
    assert 'COOL COMPANY, WRONG ROLE: "is_cool_company_wrong_role" is true when' in prompt
    # the cap is code, never asked of the prompt
    assert "cap" not in prompt.lower().replace("capital", "")


def test_old_function_fit_names_read_as_the_new_ones():
    """PUB-10: a batch queued before the rename carries it_other / not_it; the
    cap applies the same, and a rationale holding the old note is not noted twice."""
    job = _job("it_other")
    job["rationale"] = "Blurb.\nFunction: IT individual contributor, not IT leadership: capped at 4."
    apply_location_tier(job, base_score=8, record=False)
    assert job["function_fit"] == "in_department" and job["function_capped"]
    assert job["rationale"].count("capped at 4") == 1
    leader = _job("it_leadership")
    apply_location_tier(leader, base_score=8, record=False)
    assert leader["function_fit"] == "leads_department" and not leader["function_capped"]
