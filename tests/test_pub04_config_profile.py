"""PUB-04: the title gate, industries, pay, scores, model and source keys are
read from profile.yaml; pinned boards from data/companies.yaml.

Covers: a different profile's gate, hard blocks, comp points and remit wording
take effect; the mission and values scale follows the profile's range; the
hard_block_industry field (and the old is_insurance key) keep a role out of
the digest and send it to Disqualified; source keys convert at the Sheet,
queue and shard-state edges; the pinned: section survives the monthly
re-resolve; profile_loader imports no pipeline module. No network access.
"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import comp  # noqa: E402
import config  # noqa: E402
import pending  # noqa: E402
import profile_loader  # noqa: E402
import prompt_template  # noqa: E402
import scorer  # noqa: E402
import sheets  # noqa: E402
import state  # noqa: E402
import weekly_summary  # noqa: E402
from sources import watchlist  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import resolve_ats  # noqa: E402


def _profile(**sections):
    """The committed profile with some sections' fields replaced, validated."""
    data = copy.deepcopy(profile_loader.read())
    for section, fields in sections.items():
        data[section].update(fields)
    assert profile_loader.validate(data) == []
    return profile_loader._freeze(data)


# --- Title gate ------------------------------------------------------------------


FINANCE_ROLES = {
    "target_titles": ["CFO"],
    "seniority_terms": ["Director", "VP"],
    "function_terms": ["Finance", "FP&A", "Accounting"],
    "function_terms_never_alone": [],
}


def test_a_finance_profile_gates_it_out_and_fpa_in():
    gate = config.make_title_gate(_profile(roles=FINANCE_ROLES)["roles"])
    assert not gate("VP of IT")
    assert gate("Director of FP&A")
    assert gate("VP, Finance")
    assert gate("CFO")
    assert not gate("Director of Marketing")


def test_the_committed_gate_is_the_profiles():
    gate = config.make_title_gate(profile_loader.active()["roles"])
    for title in ("VP of IT", "Head of Corporate Engineering", "Chief Information Officer",
                  "Head of Enterprise Sales", "Digital Communications Director"):
        assert gate(title) == config.title_gate(title), title
    assert config.title_gate("VP of IT") and not config.title_gate("Director of FP&A")


def test_a_never_alone_word_never_counts_bare():
    roles = dict(FINANCE_ROLES, function_terms=["Finance", "Corporate Finance"],
                 function_terms_never_alone=["corporate"])
    gate = config.make_title_gate(_profile(roles=roles)["roles"])
    assert gate("Director, Corporate Finance")
    assert not gate("Director, Corporate Communications")


def test_the_watchlist_gate_reads_its_own_terms():
    roles = dict(profile_loader.read()["roles"])
    roles["watchlist_gate_terms"] = dict(roles["watchlist_gate_terms"], function=["finance"])
    gate = config.make_watchlist_gate(_profile(roles=roles)["roles"])
    assert gate("Senior Director, Finance")
    assert not gate("Senior Director, Enterprise Networking")
    # The committed one keeps Nvidia's proven score-8 role.
    assert config.watchlist_title_gate("Senior Director, Enterprise Networking")


def test_title_gated_sources_are_module_keys():
    assert config.TITLE_GATED_SOURCES == {"adzuna", "jsearch", "builtin", "ats", "startups"}
    assert set(config.TITLE_GATED_SOURCES) <= set(profile_loader.active()["sources"]["enabled"])


# --- Hard blocks -----------------------------------------------------------------


def test_a_crypto_only_profile_does_not_block_an_insurance_title():
    p = _profile(industries={"hard_block": {"crypto": ["crypto"]}})
    pattern = scorer.name_block(p["industries"]["hard_block"])
    job = {"title": "VP of IT", "company": "Acme Insurance"}
    assert scorer._is_hard_disqualify(job, pattern) is None
    assert scorer._is_hard_disqualify({"title": "VP of IT", "company": "Crypto Co"}, pattern) == "crypto"
    # The committed profile still blocks it.
    assert scorer._is_hard_disqualify(job) == "insurance"
    prompt = scorer.build_system_prompt(p)
    assert "Hard no (score 0): crypto\n" in prompt
    assert '"hard_block_industry": "<crypto, or null>"' in prompt


@pytest.mark.parametrize("reply, expected", [
    ("insurance", "insurance"), ("Gambling/Casinos", "gambling/casinos"), (None, None),
    ("null", None), ("", None), ("payday lending", "payday lending"),
])
def test_hard_block_industry_is_normalised(reply, expected):
    assert scorer._hard_block_industry({"hard_block_industry": reply}, {}) == expected


def _scored(score, **extra):
    return {"title": "VP of IT", "company": "Acme", "score": score, "rationale": "r", **extra}


def test_a_hard_block_stays_out_of_the_digest_at_any_score():
    jobs = [_scored(8, hard_block_industry="crypto"), _scored(8, is_insurance=True), _scored(8)]
    sections = scorer.filter_jobs(jobs)
    assert [j.get("hard_block_industry") or j.get("is_insurance") for j in sections["main"]] == [None]
    assert scorer.is_hard_blocked(jobs[0]) and scorer.is_hard_blocked(jobs[1])
    assert not scorer.is_hard_blocked(jobs[2])


def test_the_prefilter_names_the_keyword():
    job = scorer.score_jobs([{"title": "Director of IT", "company": "Acme Insurance"}])[0]
    assert job["score"] == 0 and job["hard_block_industry"] == "insurance"
    assert "is_insurance" not in job


def test_thresholds_come_from_the_profile():
    t = {"main_digest": 9, "worth_a_look": 6, "dream_company_main": 5}
    jobs = [_scored(8), _scored(6), _scored(5, is_dream_company=True), _scored(4, is_dream_company=True)]
    sections = scorer.filter_jobs(jobs, thresholds=t)
    assert [j["score"] for j in sections["main"]] == [5]
    assert [j["score"] for j in sections["worth_a_look"]] == [8, 6]


# --- Compensation ----------------------------------------------------------------


LOW_COMP = {"floor": 120000, "floor_adjustment": -3, "stretch": 160000, "stretch_adjustment": 2}


@pytest.mark.parametrize("pay, adjustment", [
    ("$100,000 - $119,999", -3), ("$100,000 - $120,000", 0), ("$140,000 - $159,999", 0),
    ("$140,000 - $160,000", 2), ("$150,000 - $200,000", 2),
])
def test_a_profile_with_lower_pay_points(pay, adjustment):
    p = _profile(compensation=LOW_COMP)
    job = {"comp": pay, "description": ""}
    assert comp.comp_adjustment(job, p["compensation"])[0] == adjustment


def test_the_comp_note_names_the_profiles_points():
    assert comp.comp_note(-3, 110000, LOW_COMP) == "Comp tops out at $110K, below $120K: -3."
    assert comp.comp_note(2, 170000, LOW_COMP) == "Comp tops out at $170K, $160K+: +2."
    # The test profile's floor and stretch.
    assert comp.comp_adjustment({"comp": "$80,000 - $95,000"}) == (-2, 95000)
    assert comp.comp_adjustment({"comp": "$120,000 - $150,000"}) == (1, 150000)


def test_usajobs_pay_floor_follows_the_comp_floor():
    assert profile_loader.usajobs_pay_floor() == 100000
    data = copy.deepcopy(profile_loader.read())
    data["sources"]["usajobs"]["pay_floor_from"] = 150000
    assert profile_loader.usajobs_pay_floor(profile_loader._freeze(data)) == 150000


# --- Prompt wording --------------------------------------------------------------


def test_expanded_remit_areas_render_in_the_prompt():
    # PUB-09: the remit areas are now one scoring.role_criteria entry's text.
    criteria = [dict(c) for c in profile_loader.read()["scoring"]["role_criteria"]]
    criteria[1]["criterion"] = "Expanded remit (Legal or Compliance also in scope)"
    p = _profile(scoring={"role_criteria": criteria})
    prompt = scorer.build_system_prompt(p)
    assert "+1: Expanded remit (Legal or Compliance also in scope)" in prompt
    assert "People Operations" not in prompt


@pytest.mark.parametrize("areas, text", [
    (["Legal"], "Legal"), (["Legal", "Compliance"], "Legal or Compliance"),
    (["People Operations", "HR", "Operations"], "People Operations, HR, or Operations"),
])
def test_or_list(areas, text):
    assert prompt_template.render("{{a|or_list}}", {"a": tuple(areas)}) == text


def test_the_mission_scale_follows_the_profiles_range():
    p = _profile(scoring={"mission_values": {"min": -3, "max": 3, "mission_alone": 1}})
    prompt = scorer.build_system_prompt(p)
    assert ("+3 strong fit · +2 partial fit · +1 partial fit · 0 not determinable from the posting\n"
            "-1 slight misfit · -2 slight misfit · -3 strong misfit") in prompt
    committed = scorer.SYSTEM_PROMPT
    assert ("+2 strong fit · +1 partial fit · 0 not determinable from the posting\n"
            "-1 slight misfit · -2 strong misfit") in committed


def test_the_vice_line_renders_from_the_profile():
    assert "Alcohol/vice can reach -2 when it is the company's core business." in scorer.SYSTEM_PROMPT
    p = _profile(industries={"vice_core_business": ["tobacco", "cannabis"]})
    assert "Tobacco/cannabis can reach -2 when" in scorer.build_system_prompt(p)
    p = _profile(industries={"vice_core_business": []})
    assert "core business" not in scorer.build_system_prompt(p)


# --- Model -----------------------------------------------------------------------


def test_the_scorer_and_weekly_summary_use_the_profiles_model():
    model = profile_loader.active()["scoring"]["model"]
    assert scorer.MODEL == model == "claude-sonnet-4-6"
    assert weekly_summary.READING_MODEL == model  # summary_model unset


def test_summary_model_is_optional_and_validated():
    data = copy.deepcopy(profile_loader.read())
    data["scoring"]["summary_model"] = "claude-haiku-4-5"
    assert profile_loader.validate(data) == []
    data["scoring"]["summary_model"] = ""
    assert any(e.startswith("scoring.summary_model:") for e in profile_loader.validate(data))
    del data["scoring"]["model"]
    assert "scoring.model: required field is missing" in profile_loader.validate(data)


# --- Source keys -----------------------------------------------------------------


def test_every_fetch_module_has_a_display_name():
    modules = {f.stem for f in (ROOT / "sources").glob("*.py")} - profile_loader.NON_FETCH_MODULES
    assert modules <= set(config.SOURCE_DISPLAY_NAMES)
    assert set(profile_loader.active()["sources"]["enabled"]) <= set(config.SOURCE_DISPLAY_NAMES)


@pytest.mark.parametrize("name, key, display", [
    ("Built In", "builtin", "Built In"), ("BuiltIn", "builtin", "Built In"),
    ("builtin", "builtin", "Built In"), ("Startup", "startups", "Startup"),
    ("LinkedIn", "linkedin_email", "LinkedIn"), ("Index Doc", "applied_doc", "Index Doc"),
    ("", "", ""), ("(blank)", "(blank)", "(blank)"),
])
def test_source_key_and_display(name, key, display):
    assert config.source_key(name) == key
    assert config.source_display(name) == display


def test_the_sheet_shows_the_display_name():
    row = sheets.main_row_data({"source": "builtin", "apply_link": "https://x"})
    assert row["Source"] == "Built In"
    assert sheets.main_row_data({"source": "startups"})["Source"] == "Startup"


def test_built_in_ranks_by_its_key():
    # Before PUB-04 jobs carried "Built In" and SOURCE_PRIORITY "BuiltIn", so
    # a Built In copy ranked below Adzuna and JSearch.
    assert config.source_rank("builtin") < config.source_rank("jsearch") < config.source_rank("adzuna")
    assert config.source_rank("Built In") == config.source_rank("builtin")


def test_a_queued_batch_with_display_names_loads_as_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path)
    (tmp_path / "20261001T000000000000Z-old.json").write_text(json.dumps({
        "found_at": "2026-10-01T00:00:00", "label": "old", "first_run": False,
        "jobs": [{"title": "VP, IT", "company": "Acme", "source": "Built In",
                  "merged_from": ["Startup", "LinkedIn"], "is_insurance": False}]}))
    _, jobs, _ = pending.load_pending()
    assert jobs[0]["source"] == "builtin"
    assert jobs[0]["merged_from"] == ["startups", "linkedin_email"]


def test_shard_state_written_before_pub04_reads_as_keys(tmp_path, monkeypatch):
    path = tmp_path / "shard_state.json"
    path.write_text(json.dumps({"shards": 6, "last_success": {},
                                "daily_sources": {"BuiltIn": "2026-10-24T08:55:07",
                                                  "LinkedIn": "2026-10-24T08:55:07"}}))
    monkeypatch.setattr(state, "_shard_state_file", lambda: str(path))
    loaded = state.load_shard_state(6)
    assert loaded["daily_sources"] == {"builtin": "2026-10-24T08:55:07",
                                       "linkedin_email": "2026-10-24T08:55:07"}


def test_index_doc_rows_are_matched_by_key():
    row = {"Source": "Index Doc", "Company": "", "Job Title": ""}
    assert weekly_summary.INDEX_DOC_SOURCE == "applied_doc"
    assert config.source_key(row["Source"]) == weekly_summary.INDEX_DOC_SOURCE


# --- Pinned boards ---------------------------------------------------------------


def test_watchlist_reads_pinned_boards_from_companies_yaml(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump({"pinned": {
        "workday": {"Example Labs": {"host": "h", "tenant": "examplelabs", "site": "s"}},
        "careers_pages": {"Example Robotics": "https://example.com/careers"}}}))
    workday, careers = watchlist.load_pinned(path)
    assert workday["Example Labs"]["tenant"] == "examplelabs"
    assert careers == {"Example Robotics": "https://example.com/careers"}


def test_resolve_ats_writes_pinned_back_unchanged(tmp_path):
    src = ROOT / "data" / "companies.yaml"
    path = tmp_path / "companies.yaml"
    path.write_text(src.read_text())
    existing = resolve_ats.load_existing(path)
    resolve_ats.write_yaml(path, existing["companies"], existing["unresolved"], existing["pinned"])
    assert path.read_text() == src.read_text()
    # Without the pinned argument the section would be lost: what PUB-04 fixed.
    resolve_ats.write_yaml(path, existing["companies"], existing["unresolved"])
    assert "pinned" not in yaml.safe_load(path.read_text())


# --- profile_loader stays a leaf ---------------------------------------------------


def test_profile_loader_imports_no_pipeline_module():
    code = ("import sys, profile_loader; profile_loader.load(); "
            "print(','.join(sorted(m for m in sys.modules if m in "
            "{'config','scorer','location','comp','main','pending','state','sheets','emailer',"
            "'weekly_summary','prompt_template'} or m.startswith('sources'))))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_dream_companies_read_from_the_profiles_file():
    names = profile_loader.dream_companies()
    path = ROOT / profile_loader.active()["dream_companies"]["file"]
    assert names == [line.strip() for line in path.read_text().splitlines() if line.strip()]
