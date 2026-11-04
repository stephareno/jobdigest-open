"""FIL-09: location tiers by straight-line distance.

Hybrid/onsite: within 60 mi of a commutable city → 0; within 40 mi of a
would-relocate centre or 60–100 mi from a commutable city → −2, relo;
anywhere else in the US → −5, relo; unresolvable → 0, no tag, logged.
No exemptions (FIL-11): the penalty applies whatever the company and whatever
the base score. Floor of 1. Remote US and international are unchanged."""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import config
import location
import profile_loader
import llm
import scorer
from tests.llm_stub import StubProvider
from location import (location_penalty, location_tier, needs_relo,
                      passes_location_filter, resolve_location)


def _job(loc, work_model="Hybrid", company="Acme Robotics Test Co"):
    return {"title": "VP, IT", "company": company, "location": loc,
            "work_model": work_model, "apply_link": f"https://x/{loc}"}


def _score(job, base):
    fake = {"score": base, "rationale": "x", "is_insurance": False,
            "is_cool_company_wrong_role": False, "work_model": job["work_model"]}
    with patch.object(scorer, "_score_single", return_value=fake), \
         patch.object(scorer.time, "sleep"):
        return scorer.score_jobs([job])[0]


# --- Home-metro strings in the formats sources send -----------------------------

@pytest.mark.parametrize("loc", [
    "Wheaton, DuPage County",
    "Chicago, Illinois, US",
    "Chicago, Illinois",
    "Evanston, Illinois",
    "Naperville, Illinois",
    "Waukesha, Wisconsin",
    "Carmel, Indiana",
])
@pytest.mark.parametrize("work_model", ["Hybrid", "In-office"])
def test_home_metro_strings_are_clean(loc, work_model):
    job = _job(loc, work_model)
    assert passes_location_filter(job)
    assert location_penalty(job) == 0
    assert needs_relo(job) is False


@pytest.mark.parametrize("loc", [
    "Chicago, IL", "Milwaukee, WI", "Indianapolis, IN", "Evanston, IL",
    "Carmel, IN", "Hybrid (Chicago, IL)", "Chicago IL",
])
def test_commutable_cities_are_clean(loc):
    assert location_penalty(_job(loc, "In-office")) == 0
    assert needs_relo(_job(loc, "In-office")) is False


# --- −2 tiers -------------------------------------------------------------------

@pytest.mark.parametrize("loc", [
    "Austin, TX", "Denver, CO", "Newark, CA", "San Francisco, CA",
    "Seattle, WA", "Houston, TX", "Irvine, CA", "San Diego, CA", "Miami, FL",
    # FIL-11 added Dallas-Fort Worth; the anchor cities, Plano and Arlington TX
    # are inside the 40-mile radius, so all four moved from −5 to −2. Arlington
    # TX and Newark CA were clean keeps under the pre-FIL-09 substring lists.
    "Dallas, TX", "Fort Worth, TX", "Plano, TX", "Arlington, TX",
])
def test_would_relocate_is_minus_two_with_relo(loc):
    job = _job(loc)
    assert location_tier(job).reason.startswith("would-relocate")
    assert location_penalty(job) == -2
    assert needs_relo(job) is True


def test_sixty_to_hundred_mile_band_is_minus_two():
    job = _job("Madison, WI", "In-office")
    assert location_tier(job).reason.startswith("near commute")
    assert location_penalty(job) == -2
    assert needs_relo(job) is True


# --- −5 fall-through ------------------------------------------------------------

@pytest.mark.parametrize("loc", [
    "Jamestown, NC", "Chesapeake, VA", "Jacksonville, FL",
    "Austin, MN",
    # Texas outside the DFW radius stays on the fall-through (FIL-11)
    "Waco, TX",
])
def test_elsewhere_in_us_is_minus_five_with_relo(loc):
    job = _job(loc)
    assert location_penalty(job) == -5
    assert needs_relo(job) is True


def test_newark_ca_and_arlington_tx_are_no_longer_clean_keeps():
    assert location_penalty(_job("Newark, CA")) != 0
    assert location_penalty(_job("Arlington, TX")) != 0


# --- Resolver -------------------------------------------------------------------

@pytest.mark.parametrize("loc", [
    "Austin, TX", "Austin, Texas", "Austin TX", "Austin, TX 78701",
    "Austin, TX (Hybrid)", "Hybrid - Austin, TX",
])
def test_resolver_formats(loc):
    (lat, lon), = resolve_location(loc)
    assert abs(lat - 30.27) < 0.1 and abs(lon + 97.74) < 0.1


def test_county_only_string_resolves():
    assert resolve_location("DuPage County")
    assert location_penalty(_job("DuPage County, IL")) == 0


def test_town_county_adzuna_format_disambiguates():
    # Wheaton exists in several states; the county picks Illinois's
    (lat, lon), = resolve_location("Wheaton, DuPage County")
    assert abs(lat - 41.87) < 0.1 and abs(lon + 88.11) < 0.1


@pytest.mark.parametrize("loc", ["Wheaton", "Austin", "Springfield"])
def test_ambiguous_bare_town_does_not_resolve_from_the_posting_alone(loc):
    # The posting string alone never picks among far-apart places; the
    # fallbacks below (scoring call, most populous) do.
    assert resolve_location(loc) == ()


@pytest.mark.parametrize("loc", ["United States", "", "Multiple Locations"])
def test_unresolvable_is_no_penalty_no_tag(loc):
    job = _job(loc)
    assert location_penalty(job) == 0
    assert needs_relo(job) is False
    assert location.resolve_job_location(job)[1] == "unresolved"


def test_linkedin_metro_phrases():
    assert location_penalty(_job("Greater Chicago Area")) == 0
    assert location_penalty(_job("Greater Milwaukee Area")) == 0
    assert location_penalty(_job("San Francisco Bay Area")) == -2
    assert location_penalty(_job("New York City Metropolitan Area")) == -5


def test_multi_location_string_takes_most_favourable():
    assert location_penalty(_job("Jacksonville, FL; Chicago, IL")) == 0
    assert location_penalty(_job("Jacksonville, FL; Denver, CO")) == -2


def test_resolution_methods_logged_once_per_run_with_counts(capsys):
    location.reset_location_resolutions()
    for i in range(3):
        j = _job("United States")
        j["apply_link"] = f"https://x/{i}"
        location_tier(j, record=True)
    location_tier(_job("Illinois"), record=True)
    location_tier(_job("Austin"), record=True)
    location_tier(_job("Dallas, TX"), record=True)
    location_tier(_job("Wheaton", "Hybrid") | {"loc_city": "Wheaton", "loc_state": "MD"}, record=True)
    location_tier(_job("United States", "Remote"), record=True)  # remote: not logged
    location.log_location_resolutions()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    line = out[0]
    assert "posting 1" in line
    assert "scoring call 1 {'Wheaton → Wheaton, MD': 1}" in line
    assert "most populous 1 {'Austin': 1}" in line
    assert "state majority 1 {'Illinois': 1}" in line
    assert "unresolved 3 {'United States': 3}" in line
    location.reset_location_resolutions()


def test_places_file_is_offline_and_loaded_once():
    assert os.path.exists(location.PLACES_FILE)
    with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
        location._places()
        location._places()
    assert location._places.cache_info().misses <= 1


# --- Unchanged: remote US and international -------------------------------------

@pytest.mark.parametrize("loc", ["Dallas, TX", "Austin, MN", "Remote - US", "United States"])
def test_remote_us_is_clean(loc):
    job = _job(loc, "Remote")
    assert passes_location_filter(job)
    assert location_penalty(job) == 0
    assert needs_relo(job) is False


@pytest.mark.parametrize("loc", ["London, UK", "Toronto, Canada", "Bangalore"])
@pytest.mark.parametrize("work_model", ["Hybrid", "In-office", "Remote"])
def test_international_still_disqualified(loc, work_model):
    job = _job(loc, work_model)
    assert not passes_location_filter(job)
    assert location_penalty(job) == 0


def test_us_signal_still_beats_international():
    job = _job("Paris, TX", "In-office")
    assert passes_location_filter(job)
    assert location_penalty(job) == -5


# --- No exemptions, and the floor (scorer) --------------------------------------

def _dream_job(loc, work_model="In-office"):
    """A job flagged dream both ways the old exemption recognised: the source's
    is_dream_company flag and a name on the dream-company list."""
    job = _job(loc, work_model, company=profile_loader.dream_companies()[0])
    job["is_dream_company"] = True
    return job


def test_dream_company_still_takes_the_penalty():
    job = _dream_job("Jacksonville, FL")
    assert location_penalty(job) == -5
    scored = _score(job, 6)
    assert scored["score"] == 1  # 6 − 5
    assert scored["location_penalty"] == -5
    assert scored["relo"] is True


def test_dream_company_in_san_francisco_base_nine_scores_seven():
    # FIL-11's worked example: no waiver for the dream list, none for a high
    # base score. San Francisco is a would-relocate centre, so −2.
    scored = _score(_dream_job("San Francisco, CA"), 9)
    assert scored["score"] == 7
    assert scored["location_penalty"] == -2
    assert scored["relo"] is True


def test_base_eight_and_above_still_takes_the_penalty():
    assert _score(_job("Jacksonville, FL", "In-office"), 8)["score"] == 3
    assert _score(_job("Jacksonville, FL", "In-office"), 10)["score"] == 5
    assert _score(_job("Austin, TX", "Hybrid"), 8)["score"] == 6


def test_dream_company_keeps_its_digest_shortcut_despite_the_penalty():
    # FIL-11 changes the score, not the placement: a dream role that lands
    # at 3 or above still reaches the top section (scorer.filter_jobs).
    job = _score(_job("Jacksonville, FL", "In-office"), 8)
    job["is_dream_company"] = True
    assert job["score"] == 3
    assert scorer.filter_jobs([job])["main"] == [job]


def test_base_seven_in_austin_scores_five():
    scored = _score(_job("Austin, TX", "Hybrid"), 7)
    assert scored["score"] == 5
    assert scored["relo"] is True


def test_dallas_fort_worth_is_minus_two_not_minus_five():
    scored = _score(_job("Dallas, TX", "In-office"), 7)
    assert scored["location_reason"].startswith("would-relocate (Dallas, TX")
    assert scored["location_penalty"] == -2
    assert scored["score"] == 5
    assert scored["relo"] is True


def test_base_four_minus_five_floors_at_one_and_stays_in_main_index():
    scored = _score(_job("Jacksonville, FL", "In-office"), 4)
    assert scored["score"] == 1
    assert scored["score"] > 0  # main.py routes score > 0 to the main index


def test_hard_block_zero_stays_zero():
    job = _job("Dallas, TX", "In-office", company="Acme Insurance")
    with patch.object(scorer, "_score_single") as never, patch.object(scorer.time, "sleep"):
        scored = scorer.score_jobs([job])[0]
    never.assert_not_called()
    assert scored["score"] == 0


def test_claude_zero_stays_zero_after_penalty():
    assert location.apply_location_penalty(0, -5) == 0


# --- State abbreviations that are also words (", OR" is Oregon, not "or") -------

@pytest.mark.parametrize("loc", ["Portland, OR", "Eugene, OR", "Hybrid - Portland, OR"])
def test_oregon_abbreviation_is_the_state(loc):
    assert location_penalty(_job(loc)) == -5
    assert needs_relo(_job(loc)) is True


def test_lowercase_or_still_separates_locations():
    assert location_penalty(_job("Dallas, TX or Chicago, IL")) == 0


_LARGEST_CITY = {
    "AL": "Huntsville", "AK": "Anchorage", "AZ": "Phoenix", "AR": "Little Rock",
    "CA": "Los Angeles", "CO": "Denver", "CT": "Bridgeport", "DE": "Wilmington",
    "DC": "Washington", "FL": "Jacksonville", "GA": "Atlanta", "HI": "Honolulu",
    "ID": "Boise", "IL": "Chicago", "IN": "Indianapolis", "IA": "Des Moines",
    "KS": "Wichita", "KY": "Louisville", "LA": "New Orleans", "ME": "Portland",
    "MD": "Baltimore", "MA": "Boston", "MI": "Detroit", "MN": "Minneapolis",
    "MS": "Jackson", "MO": "Kansas City", "MT": "Billings", "NE": "Omaha",
    "NV": "Las Vegas", "NH": "Manchester", "NJ": "Newark", "NM": "Albuquerque",
    "NY": "New York", "NC": "Charlotte", "ND": "Fargo", "OH": "Columbus",
    "OK": "Oklahoma City", "OR": "Portland", "PA": "Philadelphia", "RI": "Providence",
    "SC": "Charleston", "SD": "Sioux Falls", "TN": "Nashville", "TX": "Houston",
    "UT": "Salt Lake City", "VT": "Burlington", "VA": "Virginia Beach", "WA": "Seattle",
    "WV": "Charleston", "WI": "Milwaukee", "WY": "Cheyenne",
}


def test_largest_city_table_covers_every_state_and_dc():
    assert set(_LARGEST_CITY) == set(location._STATES)
    assert len(_LARGEST_CITY) == 51


@pytest.mark.parametrize("abbr", sorted(_LARGEST_CITY))
def test_largest_city_with_state_abbreviation_resolves_in_that_state(abbr):
    loc = f"{_LARGEST_CITY[abbr]}, {abbr}"
    points = resolve_location(loc)
    assert len(points) == 1, loc
    places, _ = location._places()
    in_state = [(p.lat, p.lon) for ps in places.values() for p in ps if p.state == abbr]
    assert points[0] in in_state, loc
    assert location_tier(_job(loc)).reason != "unresolved", loc


# --- Fallbacks: scoring call, most populous, state majority ----------------------

def _with_scoring(job, city, state):
    job.update({"loc_city": city, "loc_state": state})
    return job


def test_bare_town_uses_scoring_call_city_and_state():
    job = _with_scoring(_job("Wheaton"), "Wheaton", "IL")
    assert location.resolve_job_location(job)[1] == "scoring call"
    assert location_penalty(job) == 0
    # The scoring call decides, not the most-populous default (which is IL)
    assert location_penalty(_with_scoring(_job("Wheaton"), "Wheaton", "MD")) == -5
    assert location_penalty(_with_scoring(_job("Wheaton"), None, "MD")) == -5


def test_posting_location_wins_over_scoring_call():
    job = _with_scoring(_job("Jacksonville, FL"), "Chicago", "IL")
    assert location.resolve_job_location(job)[1] == "posting"
    assert location_penalty(job) == -5


def test_united_states_uses_scoring_call():
    job = _with_scoring(_job("United States"), "Austin", "TX")
    assert location.resolve_job_location(job)[1] == "scoring call"
    assert location_penalty(job) == -2


def test_scoring_call_nulls_fall_through():
    job = _with_scoring(_job("United States"), "null", None)
    assert location.resolve_job_location(job)[1] == "unresolved"
    assert location_penalty(job) == 0


def test_bare_town_without_context_takes_most_populous():
    job = _job("Austin")
    assert location.resolve_job_location(job)[1] == "most populous"
    assert location_penalty(job) == -2


@pytest.mark.parametrize("loc,penalty", [
    (", Wyoming", -5), (", Arizona", -5), ("Illinois", 0), ("California", -2),
])
def test_state_only_takes_state_majority_tier(loc, penalty):
    job = _job(loc)
    assert location.resolve_job_location(job)[1] == "state majority"
    assert location_penalty(job) == penalty
    assert needs_relo(job) is (penalty != 0)


def test_indiana_is_commutable_through_the_override():
    assert dict(location.CONFIG.state_overrides) == {"IN": 0}
    job = _job("Indiana")
    assert location_tier(job).reason == "state override (IN)"
    assert location_penalty(job) == 0
    assert needs_relo(job) is False


def test_united_states_stays_unresolved():
    job = _job("United States")
    assert location.resolve_job_location(job)[1] == "unresolved"
    assert location_penalty(job) == 0
    assert needs_relo(job) is False


def test_state_majority_covers_every_state():
    majority = location.state_majority_penalties()
    assert set(location._STATES) <= set(majority)
    for penalty, share in majority.values():
        assert penalty in (0, -2, -5) and 0 < share <= 1


def test_scoring_response_carries_city_and_state():
    assert '"city"' in scorer.SYSTEM_PROMPT and '"state"' in scorer.SYSTEM_PROMPT
    reply = ('{"score": 7, "rationale": "x", "is_insurance": false, '
             '"is_cool_company_wrong_role": false, "work_model": "Hybrid", '
             '"city": "Wheaton", "state": "MD"}')
    stub = StubProvider(lambda call: reply)
    with patch.object(llm, "_provider", stub), patch.object(scorer.time, "sleep"):
        scored = scorer.score_jobs([_job("Wheaton", "")])[0]
    assert len(stub.calls) == 1  # no extra API call for location
    assert scored["loc_city"] == "Wheaton" and scored["loc_state"] == "MD"
    assert scored["location_penalty"] == -5
    assert scored["score"] == 2


# --- Bare names: sizeable places, most favourable tier; "Washington" = DC -------

@pytest.mark.parametrize("loc,penalty", [
    ("Aurora", 0),        # Aurora IL over Aurora CO
    ("Arlington", -2),    # Arlington TX over Arlington VA
    ("Washington", -5),   # alone: Washington DC, not the state
    ("Austin", -2),       # only Austin TX is sizeable
])
def test_bare_name_takes_most_favourable_sizeable_place(loc, penalty):
    job = _job(loc)
    assert location.resolve_job_location(job)[1] == "most populous"
    assert location_penalty(job) == penalty


def test_bare_name_with_no_sizeable_place_keeps_most_populous():
    places, _ = location._places()
    wexfords = places["wexford"]
    assert all(p.population < location.CONFIG.sizeable_place_population for p in wexfords)
    (point,) = resolve_location("Wexford", most_populous=True)
    assert point == (max(wexfords, key=lambda p: p.population).lat,
                     max(wexfords, key=lambda p: p.population).lon)


def test_washington_with_country_token_is_still_the_state():
    job = _job("Washington, United States")
    assert location.resolve_job_location(job)[1] == "state majority"
    assert location_penalty(job) == -2


def test_scoring_call_still_beats_bare_name_default():
    # Bare "Aurora" defaults to Aurora IL (0); the scoring call sends it to
    # Aurora CO, inside the Denver radius (−2).
    job = _with_scoring(_job("Aurora"), "Aurora", "CO")
    assert location.resolve_job_location(job)[1] == "scoring call"
    assert location_penalty(job) == -2


# --- Pending batches queued before FIL-09 ----------------------------------------

import pending


def _legacy_row(loc, score, stored_penalty, work_model="In-office", company="Acme Robotics Test Co"):
    # Shape of a state/pending/ row written before FIL-09: score already carries
    # the flat penalty; no base_score / location_method fields.
    return {"title": "VP, IT", "company": company, "location": loc, "work_model": work_model,
            "apply_link": f"https://x/{loc}/{score}", "score": score,
            "location_penalty": stored_penalty, "relo": bool(stored_penalty),
            "rationale": "x", "is_insurance": False, "is_cool_company_wrong_role": False}


def test_legacy_pending_row_is_retiered_from_its_base():
    row = _legacy_row("Wheaton, DuPage County", 6, -2)
    assert pending.retier_legacy([row]) == 1
    assert row["base_score"] == 8
    assert row["location_penalty"] == 0
    assert row["relo"] is False
    # Wheaton is 25 mi from Chicago, so the re-tiered row also takes the GAP-07 bonus.
    assert row["home_bonus"] == 1
    assert row["score"] == 9


def test_legacy_pending_score_zero_row_is_left_alone():
    row = _legacy_row("Jacksonville, FL", 0, -2)
    before = dict(row)
    assert pending.retier_legacy([row]) == 0
    assert row == before


def test_legacy_pending_row_gets_new_penalty_and_floor():
    row = _legacy_row("Jacksonville, FL", 3, -2)  # base 5, −5 → 5−5=0 → floor 1
    pending.retier_legacy([row])
    assert row["score"] == 1 and row["location_penalty"] == -5 and row["relo"] is True
    remote = _legacy_row("Jacksonville, FL", 7, 0, work_model="Remote")
    pending.retier_legacy([remote])
    assert remote["score"] == 7


def test_already_tiered_pending_row_is_not_touched():
    row = _score(_job("Austin, TX", "Hybrid"), 7)
    assert row["score"] == 5
    assert pending.retier_legacy([row]) == 0
    assert row["score"] == 5
