"""PUB-09: the scoring rubric is the profile's (scoring.role_criteria), company
appeal and mission/values are optional, and pay, place and work model are
modifiers in code whose outcome is a number or "disqualify".

Covers: a finance profile's criteria render and the IT ones do not;
company_fit.enabled false and scoring.mission_values.enabled false remove
their blocks; nested #if blocks; role_criteria validation; criteria_hits from
a stubbed reply to the job and the rationale notes line; work_models.onsite,
elsewhere_penalty, floor_adjustment and unstated outcomes; a dealbreaker is
never emailed; location.country and unknown state codes fail validation. No
network.
"""

import copy
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import comp  # noqa: E402
import location  # noqa: E402
import profile_loader  # noqa: E402
import prompt_template  # noqa: E402
import llm  # noqa: E402
import scorer  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402

FINANCE_CRITERIA = [
    {"id": "owns_close", "points": 3, "criterion": "Owns the monthly close and the audit relationship"},
    {"id": "board_reporting", "points": 1, "criterion": "Presents to the board or its audit committee"},
    {"id": "ap_only", "points": -2, "criterion": "Accounts payable processing only"},
]
IT_LINES = ("Real decision-making scope", "Expanded remit", "Reports to CIO/COO/CEO",
            "Pure infosec/CISO scope only", "Client-facing/consulting delivery")


def _raw():
    return copy.deepcopy(profile_loader.read())


def _profile(raw):
    assert profile_loader.validate(raw) == []
    return profile_loader._freeze(profile_loader.with_defaults(raw))


def _errors_for(raw, prefix):
    return [e for e in profile_loader.validate(raw) if e.startswith(prefix)]


# --- Rubric in the prompt ----------------------------------------------------------


def test_a_finance_profile_renders_its_criteria_and_not_the_it_ones():
    raw = _raw()
    raw["scoring"]["role_criteria"] = FINANCE_CRITERIA
    prompt = scorer.build_system_prompt(_profile(raw))
    block = prompt.split("SCORING (1-10):\n", 1)[1].split("Hard disqualify", 1)[0]
    lines = block.splitlines()
    assert lines[0] == "+3: Owns the monthly close and the audit relationship"
    assert lines[1] == "+1: Presents to the board or its audit committee"
    assert lines[-1] == "-2: Accounts payable processing only"       # negatives after the positives
    for text in IT_LINES:
        assert text not in prompt
    assert "from: owns_close, board_reporting, ap_only" in prompt


def test_the_criteria_render_positives_then_negatives():
    block = scorer.SYSTEM_PROMPT.split("SCORING (1-10):\n", 1)[1].split("Hard disqualify", 1)[0]
    signs = [line[0] for line in block.splitlines() if line[:1] in "+-" and line[1:2].isdigit()]
    assert signs == ["+", "+", "+", "-"]                    # two criteria, company appeal, then one
    assert "Location is scored separately, outside this call" in block


def test_company_fit_disabled_keeps_only_the_tiers():
    raw = _raw()
    raw["company_fit"]["enabled"] = False
    prompt = scorer.build_system_prompt(_profile(raw))
    assert raw["company_fit"]["core_question"] not in prompt
    assert "These signals are what earn" not in prompt
    for signal in raw["company_fit"]["appeal_signals"]:
        assert f"- {signal}\n" not in prompt
    assert "COMPANY FIT\nThe tiers, by the problem the company solves:" in prompt
    assert "+2: Sector (see COMPANY FIT below)" in prompt and "Company appeal" not in prompt
    assert "Strong yes (+2):\nrobotics" in prompt


def test_company_fit_disabled_needs_no_appeal_wording():
    raw = _raw()
    raw["company_fit"]["enabled"] = False
    del raw["company_fit"]["core_question"], raw["company_fit"]["appeal_signals"]
    assert "COMPANY FIT\nThe tiers" in scorer.build_system_prompt(_profile(raw))
    raw["company_fit"]["enabled"] = True
    assert _errors_for(raw, "company_fit.core_question") == [
        "company_fit.core_question: required while company_fit.enabled is true"]


def test_mission_values_disabled_removes_the_block():
    raw = _raw()
    raw["scoring"]["mission_values"]["enabled"] = False
    prompt = scorer.build_system_prompt(_profile(raw))
    assert "MISSION & VALUES FIT" not in prompt and "ON ITS OWN" not in prompt
    assert "strong fit" not in prompt and "Never return 0 because of a values misfit" not in prompt
    # The reply format names no mission or values field, on or off.
    reply = prompt.split("Return ONLY this JSON:", 1)[1]
    assert "mission" not in reply.lower() and "values" not in reply.lower()
    # The prompt still reads cleanly: the criteria block runs straight into COMPANY FIT.
    assert "\n\n\nCOMPANY FIT" not in prompt and "\n\nCOMPANY FIT — " in prompt


def test_nested_if_blocks_resolve_inside_out():
    text = "{{#if a}}A{{#if b}}B{{/if}}{{#if not b}}-{{/if}}A{{/if}}."
    assert prompt_template.render(text, {"a": True, "b": False}) == "A-A."
    assert prompt_template.render(text, {"a": True, "b": True}) == "ABA."
    assert prompt_template.render(text, {"a": False, "b": True}) == "."


@pytest.mark.parametrize("criteria, message", [
    ([], "must list at least one criterion"),
    ([{"id": "a", "points": 1, "criterion": "x"}, {"id": "a", "points": 2, "criterion": "y"}], "duplicate id 'a'"),
    ([{"id": "a", "points": 6, "criterion": "x"}], "from -5 to +5, got 6"),
    ([{"id": "a", "points": 0, "criterion": "x"}], "non-zero"),
    ([{"id": "Has Space", "points": 1, "criterion": "x"}], "snake_case"),
    ([{"id": "a", "points": 1, "criterion": " "}], "one line the model can recognize"),
    ([{"id": "a", "points": 1, "criterion": "x", "weight": 2}], "unknown field"),
])
def test_role_criteria_validation(criteria, message):
    raw = _raw()
    raw["scoring"]["role_criteria"] = criteria
    errors = _errors_for(raw, "scoring.role_criteria")
    assert errors and any(message in e for e in errors), errors


def test_the_removed_fields_are_rejected():
    raw = _raw()
    raw["scoring"]["decision_scope_bonus"] = 3
    raw["roles"]["exec_titles_standalone"] = ["CIO"]
    errors = profile_loader.validate(raw)
    assert "scoring.decision_scope_bonus: unknown field (not in the profile schema; a typo?)" in errors
    assert "roles.exec_titles_standalone: renamed to roles.target_titles" in errors


def test_target_department_fills_the_function_fit_line():
    raw = _raw()
    raw["roles"]["target_department"] = "a corporate finance function"
    prompt = scorer.build_system_prompt(_profile(raw))
    assert '- "leads_department": leads a corporate finance function and owns a\n  team or a budget.' in prompt


# --- criteria_hits -------------------------------------------------------------------


def test_criteria_hits_flow_from_the_reply_to_the_job_and_the_rationale(monkeypatch):
    payload = {"score": 8, "criteria_hits": ["reports_up", "owns_decisions", "made_up"],
               "rationale": {"company": "Robot maker.", "role": "Top IT seat.", "culture": "candid"},
               "hard_block_industry": None, "is_cool_company_wrong_role": False,
               "function_fit": "leads_department", "work_model": "Remote", "city": None, "state": None,
               "hiring_company": "Acme", "website": "acme.com"}
    monkeypatch.setattr(llm, "_provider", StubProvider(lambda call: json.dumps(payload)))
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    job = {"title": "VP IT", "company": "Acme", "location": "Remote, US", "work_model": "Remote",
           "description": "", "comp": ""}
    scorer.score_jobs([job])
    assert job["criteria_hits"] == ["owns_decisions", "reports_up"]     # profile order, unknown dropped
    blurb, notes = location.split_rationale(job["rationale"])
    assert notes == "Criteria: owns decisions +3, reports up +1."
    assert "Criteria" not in blurb
    # Re-tiering never names them twice.
    location.apply_location_tier(job, base_score=job["base_score"], record=False)
    assert job["rationale"].count("Criteria:") == 1


@pytest.mark.parametrize("value, hits", [
    (None, []), ("owns_decisions, no_authority", ["owns_decisions", "no_authority"]), (7, []), ([], []),
])
def test_criteria_hits_are_normalised(value, hits):
    assert scorer.criteria_hits({"criteria_hits": value}, {}) == hits


# --- Modifiers -----------------------------------------------------------------------


@pytest.fixture
def modifiers(monkeypatch):
    """Swap in a location config and comp section built from an edited profile."""
    def apply(edit):
        raw = _raw()
        edit(raw)
        profile = _profile(raw)
        monkeypatch.setattr(location, "CONFIG", location.build_config(profile))
        monkeypatch.setattr(comp, "COMP", profile["compensation"])
        return profile
    return apply


def _job(location_text, work_model, description="", **kw):
    return {"title": "VP IT", "company": "Acme", "location": location_text, "work_model": work_model,
            "description": description, "comp": "", "rationale": "Robot maker. Top IT seat.",
            "function_fit": "leads_department", **kw}


def test_onsite_dealbreaker_scores_zero_and_leaves_remote_alone(modifiers):
    modifiers(lambda raw: raw["location"]["work_models"].update(onsite="disqualify"))
    onsite = _job("Evanston, IL", "In-office")
    assert location.apply_location_tier(onsite, base_score=9, record=False) == 0
    assert onsite["dealbreaker"] == "onsite"
    assert onsite["location_reason"] == "dealbreaker: onsite"
    assert "Dealbreaker: onsite." in onsite["rationale"]
    remote = _job("Remote, US", "Remote")
    assert location.apply_location_tier(remote, base_score=9, record=False) == 9
    assert "dealbreaker" not in remote and remote["location_reason"] == "not hybrid/onsite"
    hybrid = _job("Evanston, IL", "Hybrid")
    assert location.apply_location_tier(hybrid, base_score=8, record=False) == 9    # home bonus, untouched


def test_a_dealbreaker_is_never_emailed_and_goes_to_disqualified(modifiers):
    modifiers(lambda raw: raw["location"]["work_models"].update(onsite="disqualify"))
    onsite = _job("Evanston, IL", "In-office", is_cool_company_wrong_role=True, is_dream_company=True)
    location.apply_location_tier(onsite, base_score=9, record=False)
    sections = scorer.filter_jobs([onsite])
    assert not (sections["main"] or sections["worth_a_look"] or sections["cool_wrong_role"])
    assert scorer.is_disqualified(onsite) and onsite["score"] == 0


def test_work_model_number_is_added_before_the_tier(modifiers):
    modifiers(lambda raw: raw["location"]["work_models"].update(remote=1, onsite=-1))
    remote = _job("Remote, US", "Remote")
    assert location.apply_location_tier(remote, base_score=7, record=False) == 8
    assert "Work model: remote: +1." in remote["rationale"]
    onsite = _job("Omaha, NE", "In-office")                # -1 work model, -5 elsewhere: floor 1
    assert location.apply_location_tier(onsite, base_score=7, record=False) == 1


def test_elsewhere_dealbreaker(modifiers):
    modifiers(lambda raw: raw["location"].update(elsewhere_penalty="disqualify"))
    far = _job("Omaha, NE", "Hybrid")
    assert location.apply_location_tier(far, base_score=9, record=False) == 0
    assert far["location_reason"] == "dealbreaker: elsewhere in US"
    near = _job("Austin, TX", "Hybrid")                      # relocate tier keeps its -2
    assert location.apply_location_tier(near, base_score=9, record=False) == 7
    remote = _job("Omaha, NE", "Remote")
    assert location.apply_location_tier(remote, base_score=9, record=False) == 9


def test_floor_dealbreaker(modifiers):
    modifiers(lambda raw: raw["compensation"].update(floor_adjustment="disqualify"))
    low = _job("Remote, US", "Remote", description="Salary range: $80,000 - $95,000.")
    assert location.apply_location_tier(low, base_score=9, record=False) == 0
    assert low["dealbreaker"] == "below floor"
    assert "Dealbreaker: below floor (comp tops out at $95K, below $100K)." in low["rationale"]
    ok = _job("Remote, US", "Remote", description="Salary range: $110,000 - $120,000.")
    assert location.apply_location_tier(ok, base_score=9, record=False) == 9


def test_unstated_pay_penalty(modifiers):
    modifiers(lambda raw: raw["compensation"].update(unstated=-1))
    silent = _job("Remote, US", "Remote")
    assert location.apply_location_tier(silent, base_score=8, record=False) == 7
    assert silent["comp_adjustment"] == -1 and "No stated pay: -1." in silent["rationale"]
    stated = _job("Remote, US", "Remote", description="Salary range: $110,000 - $120,000.")
    assert location.apply_location_tier(stated, base_score=8, record=False) == 8
    # Re-tiering never names it twice.
    location.apply_location_tier(silent, base_score=8, record=False)
    assert silent["rationale"].count("No stated pay") == 1


def test_unstated_dealbreaker(modifiers):
    modifiers(lambda raw: raw["compensation"].update(unstated="disqualify"))
    silent = _job("Remote, US", "Remote")
    assert location.apply_location_tier(silent, base_score=8, record=False) == 0
    assert silent["dealbreaker"] == "no stated pay"


def test_work_model_dealbreaker_short_circuits_the_rest(modifiers):
    modifiers(lambda raw: (raw["location"]["work_models"].update(onsite="disqualify"),
                           raw["compensation"].update(floor_adjustment="disqualify")))
    job = _job("Omaha, NE", "In-office", description="Salary range: $80,000 - $95,000.")
    location.apply_location_tier(job, base_score=9, record=False)
    assert job["dealbreaker"] == "onsite" and "below floor" not in job["rationale"]


def test_a_zero_stays_zero_with_no_dealbreaker(modifiers):
    modifiers(lambda raw: raw["location"]["work_models"].update(onsite="disqualify"))
    job = _job("Evanston, IL", "In-office")
    assert location.apply_location_tier(job, base_score=0, record=False) == 0
    assert "dealbreaker" not in job


def test_outside_country_as_a_number(modifiers):
    modifiers(lambda raw: raw["location"].update(outside_country=-3))
    abroad = _job("Oslo, Norway", "Remote")
    assert location.passes_location_filter(abroad)
    assert location.apply_location_tier(abroad, base_score=8, record=False) == 5
    assert "Location: outside US: -3." in abroad["rationale"] and not abroad["relo"]


def test_outside_country_disqualify_drops_before_scoring():
    assert location.CONFIG.outside_country == "disqualify"
    assert not location.passes_location_filter(_job("Oslo, Norway", "Remote"))
    assert location.passes_location_filter(_job("Paris, TX", "Hybrid"))      # country signal wins


def test_outcome_fields_validate():
    raw = _raw()
    raw["location"]["work_models"]["hybrid"] = "maybe"
    raw["location"]["relocate_penalty"] = "disqualify"
    raw["compensation"]["floor_adjustment"] = 1
    errors = profile_loader.validate(raw)
    assert 'location.work_models.hybrid: must be an integer or "disqualify", got string \'maybe\'' in errors
    assert "compensation.floor_adjustment: is a penalty and must be negative, got 1" in errors
    assert not _errors_for(raw, "location.relocate_penalty")


def test_defaults_fill_the_new_fields():
    raw = _raw()
    for key in ("country", "outside_country", "work_models"):
        del raw["location"][key]
    del raw["compensation"]["unstated"], raw["company_fit"]["enabled"], raw["scoring"]["mission_values"]["enabled"]
    profile = _profile(raw)
    assert profile["location"]["country"] == "US" and profile["location"]["outside_country"] == "disqualify"
    assert dict(profile["location"]["work_models"]) == {"remote": 0, "hybrid": 0, "onsite": 0}
    assert profile["compensation"]["unstated"] == 0
    assert profile["company_fit"]["enabled"] is True and profile["scoring"]["mission_values"]["enabled"] is True


# --- Country and state codes ---------------------------------------------------------


def test_another_country_fails_validation_naming_the_builder():
    raw = _raw()
    raw["location"]["country"] = "CA"
    errors = _errors_for(raw, "location.country")
    assert len(errors) == 1
    assert "'CA' is not covered" in errors[0] and "holds US places only" in errors[0]
    assert "tools/build_us_places.py" in errors[0]


def test_an_unknown_state_code_fails_validation():
    raw = _raw()
    raw["location"]["relocate"].append("Nowhere, ZZ")
    errors = _errors_for(raw, "location.relocate")
    assert errors == [f"location.relocate[{len(raw['location']['relocate']) - 1}]: 'Nowhere, ZZ' does not "
                      f"resolve to a US place: 'ZZ' is not a two-letter US state code"]
    assert profile_loader.unknown_state_code("Washington, DC") is None
    assert profile_loader.unknown_state_code("Nowhere, ZZ") == "ZZ"
