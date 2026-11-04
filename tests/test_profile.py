"""PUB-01: profile.yaml loads cleanly, and the validator names the field path
of every problem, all at once.

Covers: the committed profile loads into a read-only tree; a missing required
field, a wrong type, a positive penalty, an unknown source and a metro that
does not resolve each produce an error naming the field path; several problems
are reported together; `python main.py profile-check` exits 0 / 1 with no
secrets set; and every value in the profile still matches the code constant it
was copied from (the code reads those values itself until PUB-03 to PUB-05,
so the two must not drift). No network access.
"""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import comp  # noqa: E402,F401
import config  # noqa: E402
import location  # noqa: E402
import profile_loader  # noqa: E402


@pytest.fixture
def raw():
    """The committed profile as plain dicts, a fresh copy per test."""
    return copy.deepcopy(profile_loader.read())


def _errors_for(raw_data, path):
    return [e for e in profile_loader.validate(raw_data) if e.startswith(path + ":")]


# --- The committed profile ---------------------------------------------------


def test_committed_profile_loads_cleanly():
    p = profile_loader.load()
    assert p["candidate"]["display_name"] == "Sam Testcase"
    assert p["location"]["home"] == "Chicago, IL"


def test_loaded_profile_is_read_only():
    p = profile_loader.load()
    with pytest.raises(TypeError):
        p["location"]["home"] = "Boston, MA"
    assert isinstance(p["location"]["commutable"], tuple)


def test_committed_profile_has_no_errors(raw):
    assert profile_loader.validate(raw) == []


# --- One error per kind, each naming its path --------------------------------


def test_missing_required_field(raw):
    del raw["compensation"]["floor"]
    assert _errors_for(raw, "compensation.floor") == ["compensation.floor: required field is missing"]


def test_missing_section(raw):
    del raw["delivery"]
    assert _errors_for(raw, "delivery") == ["delivery: required field is missing"]


def test_wrong_type(raw):
    raw["location"]["commutable_radius_miles"] = "sixty"
    errs = _errors_for(raw, "location.commutable_radius_miles")
    assert len(errs) == 1 and "must be an integer" in errs[0]


def test_boolean_is_not_an_integer(raw):
    raw["scoring"]["max_score"] = True
    assert "must be an integer" in _errors_for(raw, "scoring.max_score")[0]


def test_list_item_wrong_type(raw):
    raw["roles"]["seniority_terms"][2] = 7
    assert "must be a non-blank string" in _errors_for(raw, "roles.seniority_terms[2]")[0]


def test_positive_penalty(raw):
    raw["location"]["relocate_penalty"] = 2
    assert _errors_for(raw, "location.relocate_penalty") == [
        "location.relocate_penalty: is a penalty and must be negative, got 2"]


def test_negative_bonus(raw):
    raw["compensation"]["stretch_adjustment"] = -1
    assert "is a bonus and must be positive" in _errors_for(raw, "compensation.stretch_adjustment")[0]


def test_unknown_source(raw):
    raw["sources"]["enabled"]["monster"] = True
    errs = _errors_for(raw, "sources.enabled.monster")
    assert len(errs) == 1 and "no such source (no sources/monster.py)" in errs[0]


def test_source_missing_from_enabled(raw):
    del raw["sources"]["enabled"]["usajobs"]
    assert "missing" in _errors_for(raw, "sources.enabled.usajobs")[0]


def test_gated_source_must_be_a_known_source(raw):
    raw["roles"]["title_gated_sources"].append("monster")
    assert _errors_for(raw, "roles.title_gated_sources[5]")


def test_bad_metro(raw):
    raw["location"]["relocate"][3] = "Atlantis, ZZ"
    errs = _errors_for(raw, "location.relocate[3]")
    assert len(errs) == 1 and "does not resolve to a US place" in errs[0]


def test_ambiguous_metro(raw):
    raw["location"]["relocate"][0] = "Springfield"
    assert _errors_for(raw, "location.relocate[0]")


def test_home_must_be_commutable(raw):
    raw["location"]["commutable"].remove("Chicago, IL")
    assert "must also be listed in location.commutable" in _errors_for(raw, "location.home")[0]


def test_unknown_field(raw):
    raw["location"]["hom"] = "Washington, DC"
    assert "unknown field" in _errors_for(raw, "location.hom")[0]


def test_floor_must_be_below_stretch(raw):
    raw["compensation"]["floor"] = 250_000
    assert "must be less than compensation.stretch" in _errors_for(raw, "compensation.floor")[0]


def test_bad_timezone_and_weekday(raw):
    raw["delivery"]["timezone"] = "Eastern"
    raw["delivery"]["digest_days"][0] = "Sundae"
    assert _errors_for(raw, "delivery.timezone")
    assert _errors_for(raw, "delivery.digest_days[0]")


def test_never_alone_word_listed_bare(raw):
    raw["roles"]["function_terms"].append("Digital")
    assert _errors_for(raw, "roles.function_terms_never_alone[1]")


def test_function_cap_must_stay_below_worth_a_look(raw):
    raw["scoring"]["function_cap"] = 5
    assert _errors_for(raw, "scoring.function_cap")


def test_wrong_version(raw):
    raw["version"] = 2
    assert _errors_for(raw, "version")


# --- All at once -------------------------------------------------------------


def test_multiple_errors_reported_together(raw):
    del raw["candidate"]["display_name"]
    raw["location"]["outer_band_miles"] = "far"
    raw["scoring"]["pure_infosec_penalty"] = 2
    raw["sources"]["enabled"]["monster"] = True
    raw["location"]["commutable"][1] = "Atlantis, ZZ"
    with pytest.raises(profile_loader.ProfileError) as exc:
        profile_loader.load(_write(raw))
    paths = {e.split(":", 1)[0] for e in exc.value.errors}
    assert {"candidate.display_name", "location.outer_band_miles", "scoring.pure_infosec_penalty",
            "sources.enabled.monster", "location.commutable[1]"} <= paths
    assert f"{len(exc.value.errors)} problem(s)" in str(exc.value)


def test_a_bad_value_is_reported_once(raw):
    # A wrong type is not repeated by the cross-field check that reads it.
    raw["compensation"]["floor"] = "lots"
    assert len([e for e in profile_loader.validate(raw) if e.startswith("compensation.")]) == 1


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(profile_loader.ProfileError, match="file not found"):
        profile_loader.load(str(tmp_path / "nope.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("candidate: [unclosed\n")
    with pytest.raises(profile_loader.ProfileError, match="not valid YAML"):
        profile_loader.load(str(bad))


_TMP = []


def _write(data):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True)
    _TMP.append(path)
    return path


def teardown_module():
    for path in _TMP:
        os.unlink(path)


# --- profile-check -----------------------------------------------------------


def _profile_check():
    # No secrets in the environment: profile-check must not need any.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "JOB_DIGEST_PROFILE": os.environ["JOB_DIGEST_PROFILE"]}
    return subprocess.run([sys.executable, "main.py", "profile-check"], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def test_profile_check_passes_without_secrets():
    result = _profile_check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Home metro:  Chicago, IL" in result.stdout
    assert "floor $100,000" in result.stdout


def test_profile_check_fails_on_an_invalid_profile(monkeypatch, capsys, raw):
    raw["location"]["elsewhere_penalty"] = 5
    assert profile_loader.check(_write(raw)) == 1
    assert "location.elsewhere_penalty: is a penalty" in capsys.readouterr().out


# --- The profile mirrors the code it was copied from -------------------------
# Retired in PUB-05: every section is now read by the code itself, so there is
# no copy left to mirror. The location tiers are covered by
# tests/test_pub03_location_config.py, the gate, industries, pay and scores by
# tests/test_pub04_config_profile.py, sources and delivery by
# tests/test_pub05_delivery_sources.py.


def test_wellfound_stays_parked():
    assert "parked" in (ROOT / "main.py").read_text().lower()
    assert not profile_loader.load()["sources"]["enabled"]["wellfound"]


# The scoring and weekly-summary prompts are rendered from the profile since
# PUB-02; tests/test_scorer_prompt.py checks them against the frozen v1 text.
