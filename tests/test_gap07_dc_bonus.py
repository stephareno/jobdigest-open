"""GAP-07: +1 for a hybrid or in-office role near the home metro.

A hybrid/in-office role whose resolved posting location is within
location.home_bonus_radius_miles (30) of location.home scores one higher (home
is Chicago on the test profile). Remote roles get nothing however local the
company is, and the other commutable metros get nothing. Applied after the location tier, before the floor and the 10 cap.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import location
import scorer
from location import home_bonus, miles_between


def _job(loc, work_model="Hybrid", company="Acme Robotics Test Co"):
    return {"title": "VP, IT", "company": company, "location": loc,
            "work_model": work_model, "apply_link": f"https://x/{loc}/{work_model}"}


def _score(job, base):
    fake = {"score": base, "rationale": "x", "is_insurance": False,
            "is_cool_company_wrong_role": False, "work_model": job["work_model"]}
    with patch.object(scorer, "_score_single", return_value=fake), \
         patch.object(scorer.time, "sleep"):
        return scorer.score_jobs([job])[0]


# --- Inside the radius, hybrid/in-office: +1 -----------------------------------

@pytest.mark.parametrize("loc", [
    "Chicago, IL", "Evanston, IL", "Naperville, IL", "Oak Park, IL",
    "Schaumburg, IL", "Hybrid (Chicago, IL)", "Skokie, Illinois",
])
@pytest.mark.parametrize("work_model", ["Hybrid", "In-office"])
def test_inside_radius_hybrid_gets_the_bonus(loc, work_model):
    job = _job(loc, work_model)
    assert home_bonus(job) == location.CONFIG.home_bonus == 1
    scored = _score(job, 6)
    assert scored["home_bonus"] == 1
    assert scored["location_penalty"] == 0
    assert scored["score"] == 7


def test_the_done_condition_evanston_hybrid_beats_the_same_role_remote():
    hybrid = _score(_job("Evanston, IL", "Hybrid"), 7)
    remote = _score(_job("Evanston, IL", "Remote"), 7)
    assert hybrid["score"] == 8
    assert remote["score"] == 7
    assert hybrid["score"] == remote["score"] + 1


# --- Remote: nothing, however local the company is --------------------------------

@pytest.mark.parametrize("loc", ["Chicago, IL", "Evanston, IL", "Remote (Chicago, IL)"])
def test_remote_inside_the_radius_gets_nothing(loc):
    job = _job(loc, "Remote")
    assert home_bonus(job) == 0
    scored = _score(job, 6)
    assert scored["home_bonus"] == 0
    assert scored["score"] == 6


def test_unknown_work_model_gets_nothing():
    # The scorer settles work_model from the JD before the bonus is applied;
    # anything that is still not hybrid/onsite by then earns nothing.
    assert home_bonus(_job("Chicago, IL", "Unknown")) == 0


# --- Outside the radius: nothing, penalty-free commutable band or not ----------

def test_45_miles_out_hybrid_gets_nothing():
    job = _job("Kenosha, WI")  # ~55 mi: commutable (no penalty), no bonus
    (point,) = location.resolve_location("Kenosha, WI")
    assert 40 < miles_between(point, location.CONFIG.home.point) < 60
    assert home_bonus(job) == 0
    scored = _score(job, 6)
    assert scored["location_penalty"] == 0
    assert scored["home_bonus"] == 0
    assert scored["score"] == 6


@pytest.mark.parametrize("loc", [
    "Milwaukee, WI", "Indianapolis, IN", "Waukesha, WI", "Carmel, IN",
    "Fishers, IN", "West Allis, WI",
])
def test_the_other_commutable_metros_get_nothing(loc):
    job = _job(loc)
    assert home_bonus(job) == 0
    scored = _score(job, 6)
    assert scored["location_penalty"] == 0
    assert scored["score"] == 6


@pytest.mark.parametrize("loc", ["Austin, TX", "Jacksonville, FL", "Denver, CO"])
def test_penalised_tiers_are_unchanged(loc):
    assert home_bonus(_job(loc)) == 0


# --- Interaction with the floor, the cap and score 0 ---------------------------

def test_the_bonus_never_lifts_a_score_above_ten():
    scored = _score(_job("Chicago, IL"), 10)
    assert scored["home_bonus"] == 1
    assert scored["score"] == 10


def test_a_disqualified_job_stays_zero():
    assert location.apply_location_penalty(0, 0, 1) == 0


def test_the_floor_still_holds():
    assert location.apply_location_penalty(2, -5, 0) == 1


# --- Tiers, radii and penalties are untouched ----------------------------------

def test_tiers_and_penalties_are_unchanged():
    assert location.CONFIG.commutable_radius_miles == 60
    assert location.CONFIG.outer_band_miles == 100
    assert location.CONFIG.relocate_radius_miles == 40
    assert (location.CONFIG.relocate_penalty, location.CONFIG.outer_band_penalty,
            location.CONFIG.elsewhere_penalty) == (-2, -2, -5)
    assert location.CONFIG.home_bonus_radius_miles == 30


def test_the_bonus_is_logged_on_the_reason():
    scored = _score(_job("Evanston, IL"), 6)
    assert "home bonus +1" in scored["location_reason"]
    assert "home bonus" not in _score(_job("Milwaukee, WI"), 6)["location_reason"]
