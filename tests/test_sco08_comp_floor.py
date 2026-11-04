"""SCO-08: comp is judged in code on the top of the stated range: -2 below
$100,000, +1 at $150,000 and up (the test profile), nothing between or when none is stated.

Covers: which pay text counts as stated (ranges, labelled single figures,
several geographic ranges) and which does not (hourly, minimums with no top,
$M/$B amounts, Adzuna's predicted salaries); the penalty sits with the
location tier and the home bonus, before the floor of 1 and the cap of 10; a
0 stays 0; the digest rationale names it once; the comp field shows the full
stated range; no comp rule is left in the prompt or config. No network access.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import comp  # noqa: E402
from location import apply_location_tier  # noqa: E402


@pytest.mark.parametrize("text, top", [
    ("The base salary range for this position is $110,500 - $147,300 annually.", 147_300),
    ("Pay range: $150K–$180K", 180_000),
    ("Salary $150-175K plus bonus", 175_000),
    ("$185,000 to $240,000 USD", 240_000),
    ("We raised $50M in Series B. Compensation: $160,000 base.", 160_000),
    ("Bethesda, MD Pay Range: $151,100-$258,000 Annually; Remote Pay Range: $146,000-$248,000", 258_000),
])
def test_stated_pay_is_read(text, top):
    assert comp.stated_comp_top({"description": text}) == top


@pytest.mark.parametrize("text", [
    "$2B in assets under management",
    "$45 - $60 per hour",
    "salary $200,000+",
    "401k match up to $5,000",
    "Company revenue $120,000,000",
    "$1.5M budget",
    "Competitive compensation and equity.",
])
def test_no_stated_annual_pay(text):
    assert comp.stated_comp_top({"description": text}) is None


def test_comp_field_is_read_unless_the_source_predicted_it():
    stated = {"comp": "$80,000 – $95,000", "description": ""}
    assert comp.comp_adjustment(stated) == (-2, 95_000)
    assert comp.comp_adjustment({**stated, "comp_is_predicted": True}) == (0, None)


def _job(score_text="", rationale="Strong fit; mission read.", **kw):
    return {"title": "Director of IT", "company": "Acme", "location": "Remote, US",
            "work_model": "Remote", "description": score_text, "comp": "",
            "rationale": rationale, **kw}


LOW = "Pay range: $80,000 - $97,000 annually."


def test_low_stated_comp_takes_two_and_is_named():
    job = _job(LOW)
    assert apply_location_tier(job, base_score=6, record=False) == 4
    assert job["comp_adjustment"] == -2
    assert job["rationale"] == "Strong fit; mission read.\nComp tops out at $97K, below $100K: -2."
    assert job["comp"] == "$80,000 – $97,000"


@pytest.mark.parametrize("text", ["", "Pay range: $110,000 - $125,000", "Pay range: $120,000 - $149,999",
                                  "Salary: $150,000+"])
def test_no_stated_comp_or_a_top_between_the_thresholds_is_unchanged(text):
    job = _job(text)
    assert apply_location_tier(job, base_score=6, record=False) == 6
    assert job["comp_adjustment"] == 0
    assert job["rationale"] == "Strong fit; mission read."


@pytest.mark.parametrize("text, top", [("Pay range: $120,000 - $150,000", "$150K"),
                                       ("Base salary $300K-$380K", "$380K")])
def test_top_at_stretch_and_up_takes_one(text, top):
    job = _job(text)
    assert apply_location_tier(job, base_score=6, record=False) == 7
    assert job["comp_adjustment"] == 1
    assert job["rationale"].endswith(f"Comp tops out at {top}, $150K+: +1.")


def test_plus_one_never_lifts_past_the_cap():
    assert apply_location_tier(_job("Pay range: $250,000 - $300,000"), base_score=10, record=False) == 10


def test_comp_field_shows_the_full_stated_range():
    marriott = _job("Remote Pay Range: $146,000-$248,000 Annually "
                    "Bethesda, MD Pay Range: $151,100-$258,000 Annually")
    apply_location_tier(marriott, base_score=6, record=False)
    assert marriott["comp"] == "$146,000 – $258,000"
    assert marriott["comp_adjustment"] == 1
    # A source range with a top is kept as given; a minimum-only one is filled in.
    adzuna = _job("", comp="$190,000 – $220,000")
    apply_location_tier(adzuna, base_score=6, record=False)
    assert adzuna["comp"] == "$190,000 – $220,000"
    partial = _job("Salary range $160,000 - $210,000", comp="$160,000+")
    apply_location_tier(partial, base_score=6, record=False)
    assert partial["comp"] == "$160,000 – $210,000"


def test_no_comp_rule_left_in_the_prompt_or_config():
    import config
    import re
    import scorer
    prompt = scorer.SYSTEM_PROMPT
    assert not re.search(r"\$\d", prompt)
    assert not hasattr(config, "MIN_COMP") and not hasattr(config, "COMP_THRESHOLD")


def test_floor_of_one_and_zero_stays_zero():
    assert apply_location_tier(_job(LOW), base_score=2, record=False) == 1
    zero = _job(LOW, rationale="Auto-disqualified: insurance industry")
    assert apply_location_tier(zero, base_score=0, record=False) == 0
    assert "Comp tops out" not in zero["rationale"]


def test_stacks_with_the_location_tier_before_the_floor():
    # Hybrid in Omaha: -5 relo tier, then -2 comp, floored at 1.
    job = _job(LOW, location="Omaha, NE", work_model="Hybrid")
    assert apply_location_tier(job, base_score=9, record=False) == 2
    assert (job["location_penalty"], job["comp_adjustment"]) == (-5, -2)


def test_with_the_home_bonus_before_the_cap():
    job = _job(LOW, location="Evanston, IL", work_model="Hybrid")
    assert apply_location_tier(job, base_score=10, record=False) == 9   # 10 + 1 - 2
    assert job["home_bonus"] == 1


def test_re_tiering_names_the_penalty_once():
    job = _job(LOW)
    apply_location_tier(job, base_score=6, record=False)
    apply_location_tier(job, base_score=6, record=False)
    assert job["rationale"].count("Comp tops out") == 1
    assert job["score"] == 4
