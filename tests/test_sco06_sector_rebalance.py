# SCO-06: sector rebalance in the scoring prompt and the config reference lists.
import json
import os
import re

os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import config  # noqa: E402
import profile_loader  # noqa: E402
import llm  # noqa: E402
import scorer  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402

PROMPT = scorer.SYSTEM_PROMPT
INDUSTRIES = profile_loader.active()["industries"]


def _tier(label):
    """Text of one COMPANY FIT tier, from its label to the next tier label."""
    m = re.search(rf"{re.escape(label)}(.*?)(?:\n(?:Strong yes|Lean in|Neutral|Deprioritize|Hard no)\b|\n\n)",
                  PROMPT, re.S)
    assert m, f"tier {label!r} missing from prompt"
    return " ".join(m.group(1).split())


def test_health_and_life_sciences_moved_to_lean_in():
    strong, lean = _tier("Strong yes (+2):"), _tier("Lean in (+1):")
    for sector in ("healthcare tech", "biotech", "pharma", "life sciences"):
        assert sector in lean
        assert sector not in strong


def test_new_strong_yes_sectors():
    strong = _tier("Strong yes (+2):")
    for sector in ("robotics", "space and aerospace", "materials science",
                   "advanced manufacturing", "physical sciences", "government agencies"):
        assert sector in strong
    # "science" alone is gone; only the qualified forms remain
    assert not re.search(r"(?<!physical )(?<!life )\bscience\b(?! and)", strong + _tier("Lean in (+1):"))


def test_sector_lean_order():
    assert "robotics > space/aerospace > materials science >" in " ".join(PROMPT.split())


def test_values_factor_has_no_sector_exception():
    flat = " ".join(PROMPT.split())
    assert "applies the same way to every sector" in flat
    assert "A mission that does good" in flat
    # no sector-specific carve-out of the round-1 kind
    assert "is a sector read" not in flat
    assert "earns no values credit" not in flat


def test_mission_earns_credit_on_its_own():
    flat = " ".join(PROMPT.split())
    assert "A mission that does good earns +1 ON ITS OWN" in flat
    assert "The factor is 0 only when the posting and company give no signal of either" in flat


def test_hospitals_neutral_on_sector():
    assert "Hospitals and health systems are also 0 on sector" in _tier("Neutral (0):")
    assert "hospitals and health systems" in INDUSTRIES["neutral"]


def test_hiring_company_judged_not_the_recruiter():
    flat = " ".join(PROMPT.split())
    assert "Judge industry, sector and values on that hiring company" in flat
    assert "applies only when the hiring company is itself a staffing firm" in flat
    assert "Each industry penalty applies at most once per role" in flat
    assert '"hiring_company"' in PROMPT


def test_hiring_company_is_a_proper_name_or_null():
    flat = " ".join(PROMPT.split())
    assert "must be the employer's proper name" in flat
    assert "never a description of one" in flat
    assert '"hiring_company": "<the employer\'s proper name, or null>"' in PROMPT


def test_hiring_company_parsed_onto_the_job(monkeypatch):
    payload = {"score": 6, "rationale": "r", "is_insurance": False,
               "is_cool_company_wrong_role": False, "work_model": "Remote",
               "city": None, "state": None, "hiring_company": "Texas Children's Hospital"}
    monkeypatch.setattr(llm, "_provider", StubProvider(lambda call: json.dumps(payload)))
    assert scorer._score_single({"title": "Deputy CIO", "company": "Insight Global"})[
        "hiring_company"] == "Texas Children's Hospital"
    payload["hiring_company"] = None
    assert scorer._score_single({"title": "t", "company": "c"})["hiring_company"] == ""


def test_no_hiring_company_column():
    """FIL-13: the employer goes in Company ("<employer> (via <poster>)"),
    never in a column of its own, written or read."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "sheets.py").read_text()
    assert "Hiring Company" not in src


def test_banks_and_corporate_real_estate_lean_in():
    lean = _tier("Lean in (+1):")
    assert "large national/global banks" in lean
    assert "corporate/institutional real estate" in lean
    assert "banks" not in _tier("Strong yes (+2):")
    assert "large national/global banks" in INDUSTRIES["lean_in"]


def test_deprioritize_matches_prd():
    dep = _tier("Deprioritize (-1, never disqualifying):")
    assert "hospital" not in dep
    for item in ("adtech", "surveillance", "oil & gas", "B2C",
                 "staffing/HR services", "regional banks", "real estate",
                 "local/regional media", "client-facing consulting delivery"):
        assert item in dep


def test_ai_infrastructure_is_neutral_in_config():
    # PUB-04: the tiers are the profile's industries lists (config.COMPANY_FIT_*
    # was deleted); the neutral AI tier is industries.lowest_interest.
    for item in ("AI infrastructure", "frontier labs"):
        assert item not in INDUSTRIES["strong_yes"]
        assert item in INDUSTRIES["lowest_interest"]
    for item in ("biotech", "pharma", "healthcare tech", "life sciences"):
        assert item in INDUSTRIES["lean_in"]
        assert item not in INDUSTRIES["strong_yes"]
    assert "science" not in INDUSTRIES["strong_yes"]


def test_json_output_shape_unchanged():
    # PUB-04: is_insurance became hard_block_industry (one of industries.hard_block).
    for key in ('"score"', '"rationale"', '"hard_block_industry"', '"is_cool_company_wrong_role"',
                '"work_model"', '"city"', '"state"'):
        assert key in PROMPT
