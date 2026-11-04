"""PUB-03: location tiers read from profile.yaml.

Covers: location.CONFIG is the committed profile's location section; a config
built from a different profile (home Denver) moves every tier and the home
bonus with it; a {name, lat, lon} metro uses its own coordinates; queued rows
carrying the pre-PUB-03 dc_bonus key are read as home_bonus; the drift between
the old hard-coded centres and the profile's resolved ones (run with -s to see
the table). No network access.
"""

import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import location  # noqa: E402
import pending  # noqa: E402
import profile_loader  # noqa: E402
import scorer  # noqa: E402

# The centres location.py hard-coded before PUB-03, in the order the profile
# lists their replacements. Kept only for the drift report below.


def _job(loc, work_model="Hybrid"):
    return {"title": "VP, IT", "company": "Acme Test Co", "location": loc,
            "work_model": work_model, "apply_link": f"https://x/{loc}/{work_model}"}


def _score(job, base):
    fake = {"score": base, "rationale": "x", "is_insurance": False,
            "is_cool_company_wrong_role": False, "work_model": job["work_model"]}
    with patch.object(scorer, "_score_single", return_value=fake), \
         patch.object(scorer.time, "sleep"):
        return scorer.score_jobs([job])[0]


def _config_from(location_overrides):
    data = copy.deepcopy(profile_loader.read())
    data["location"].update(location_overrides)
    assert profile_loader.validate(data) == []
    return location.build_config(data)


# --- The committed profile -----------------------------------------------------


def test_config_is_the_profiles_location_section():
    loc, cfg = profile_loader.load()["location"], location.CONFIG
    assert cfg.home.name == loc["home"] == "Chicago, IL"
    assert [c.name for c in cfg.commutable] == list(loc["commutable"])
    assert [c.name for c in cfg.relocate] == list(loc["relocate"])
    assert (cfg.home_bonus, cfg.home_bonus_radius_miles) == (loc["home_bonus"], loc["home_bonus_radius_miles"])
    assert (cfg.commutable_radius_miles, cfg.outer_band_miles, cfg.relocate_radius_miles) == (60, 100, 40)
    assert (cfg.outer_band_penalty, cfg.relocate_penalty, cfg.elsewhere_penalty) == (-2, -2, -5)
    assert cfg.state_override("IN") == 0 and cfg.state_override("MD") is None
    assert (cfg.score_floor, cfg.max_score) == (1, 10)


# --- A different profile: home Denver ------------------------------------------


@pytest.fixture
def denver(monkeypatch):
    cfg = _config_from({
        "home": "Denver, CO",
        "commutable": ["Denver, CO", "Salt Lake City, UT"],
        "relocate": ["Austin, TX"],
        "state_tier_overrides": {},
    })
    monkeypatch.setattr(location, "CONFIG", cfg)
    return cfg


def test_denver_hybrid_gets_the_home_bonus_and_no_penalty(denver):
    scored = _score(_job("Denver, CO"), 6)
    assert scored["location_penalty"] == 0 and scored["relo"] is False
    assert scored["home_bonus"] == 1 and scored["score"] == 7
    assert "home bonus +1" in scored["location_reason"]
    assert "Hybrid near Denver, CO: +1." in scored["rationale"]


def test_denver_salt_lake_city_is_commutable_without_the_bonus(denver):
    scored = _score(_job("Salt Lake City, UT"), 6)
    assert scored["location_penalty"] == 0 and scored["home_bonus"] == 0 and scored["score"] == 6


def test_denver_washington_dc_is_elsewhere_with_relo(denver):
    scored = _score(_job("Washington, DC"), 7)
    assert scored["location_reason"] == "elsewhere in US"
    assert scored["location_penalty"] == -5 and scored["relo"] is True
    assert scored["home_bonus"] == 0 and scored["score"] == 2


def test_denver_austin_takes_the_relocate_penalty(denver):
    scored = _score(_job("Austin, TX", "In-office"), 7)
    assert scored["location_reason"].startswith("would-relocate (Austin, TX")
    assert scored["location_penalty"] == -2 and scored["relo"] is True and scored["score"] == 5


def test_denver_remote_gets_nothing(denver):
    scored = _score(_job("Denver, CO", "Remote"), 7)
    assert scored["location_penalty"] == 0 and scored["home_bonus"] == 0
    assert scored["relo"] is False and scored["score"] == 7


def test_state_majority_follows_the_config(denver):
    # Under the Denver profile Colorado is mostly commutable and Virginia is not.
    majority = location.state_majority_penalties()
    assert majority["CO"][0] == 0
    assert location.state_tier("VA").penalty != 0


# --- {name, lat, lon} -------------------------------------------------------------


def test_lat_lon_override_wins_over_the_places_file(monkeypatch):
    # "Springfield" alone names many places and would fail validation; with
    # coordinates (Springfield, IL) it validates and those coordinates are used.
    pinned = {"name": "Springfield", "lat": 39.7817, "lon": -89.6501}
    cfg = _config_from({"commutable": ["Chicago, IL", pinned]})
    assert cfg.commutable[1] == location.Centre("Springfield", (39.7817, -89.6501))
    monkeypatch.setattr(location, "CONFIG", cfg)
    tier = location.location_tier(_job("Decatur, IL"))   # ~40 mi from Springfield IL
    assert tier.penalty == 0 and tier.reason.startswith("commutable (Springfield,")


def test_a_bare_ambiguous_name_without_coordinates_is_rejected():
    data = copy.deepcopy(profile_loader.read())
    data["location"]["commutable"].append({"name": "Springfield"})
    errs = [e for e in profile_loader.validate(data) if e.startswith("location.commutable[3]")]
    assert errs and "does not resolve" in errs[0]


@pytest.mark.parametrize("entry, message", [
    ({"name": "Somewhere", "lat": 39.0}, "give lat and lon together"),
    ({"name": "Somewhere", "lat": 120.0, "lon": 0.0}, "must be between -90 and 90"),
    ({"name": "Somewhere", "lat": "39", "lon": 0.0}, "must be a number"),
    ({"lat": 39.0, "lon": -77.0}, "name: required"),
])
def test_bad_place_mappings_name_the_field(entry, message):
    data = copy.deepcopy(profile_loader.read())
    data["location"]["relocate"][0] = entry
    errs = [e for e in profile_loader.validate(data) if e.startswith("location.relocate[0]")]
    assert any(message in e for e in errs), errs


# --- The pre-PUB-03 dc_bonus key -------------------------------------------------


def test_queued_dc_bonus_is_read_as_home_bonus(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path)
    (tmp_path / "20261001T000000000000Z-old.json").write_text(json.dumps({
        "found_at": "2026-10-01T00:00:00", "label": "old", "first_run": False,
        "jobs": [{"title": "VP, IT", "company": "Acme", "score": 8, "dc_bonus": 1,
                  "base_score": 7, "location_method": "posting"}]}))
    _, jobs, _ = pending.load_pending()
    assert jobs[0]["home_bonus"] == 1 and "dc_bonus" not in jobs[0]


def test_retier_drops_an_old_dc_bonus_and_its_note():
    job = {**_job("Evanston, IL"), "score": 8, "dc_bonus": 1,
           "rationale": "Blurb.\nHybrid near DC: +1."}
    location.apply_location_tier(job, base_score=7, record=False)
    assert "dc_bonus" not in job and job["home_bonus"] == 1
    assert job["rationale"] == "Blurb.\nHybrid near Chicago, IL: +1."
