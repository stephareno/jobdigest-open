"""PUB-07b: the leftovers before the public staging export.

Covers: the scoring prompt's sample domain is example.com; the "same -1 as in
SCORING" note renders only when a deprioritize entry is also a negative SCORING
criterion, and the staffing sentence only when the profile names staffing;
calibrate reports API calls (retries included) apart from postings scored;
the export's default --repo is the public repository.
"""

import copy
from pathlib import Path

import calibrate
import profile_loader
import prompt_template

ROOT = Path(__file__).resolve().parent.parent


def _profile(path=ROOT / "tests" / "fixtures" / "profile_test.yaml"):
    return copy.deepcopy(profile_loader._thaw(profile_loader.load(str(path))))


def _prompt(profile):
    return prompt_template.render_file("scoring_system.txt", profile)


def test_the_sample_domain_is_example_com():
    text = (ROOT / "prompts" / "scoring_system.txt").read_text()
    assert '"example.com" or "usda.gov"' in text and "dragos" not in text


def test_the_apply_once_note_follows_a_deprioritize_entry_that_is_also_a_criterion():
    profile = _profile()
    assert "as in SCORING above" not in _prompt(profile)
    profile["scoring"]["role_criteria"].append(
        {"id": "consulting_delivery", "points": -1,
         "criterion": "Client-facing/consulting delivery (external billable work)"})
    assert prompt_template.overlaps(profile)["scored_twice"] == ["client-facing consulting delivery"]
    assert "client-facing consulting delivery (the\nsame -1 as in SCORING above — apply it once)." in _prompt(profile)


def test_a_positive_criterion_never_triggers_the_apply_once_note():
    profile = _profile()
    profile["scoring"]["role_criteria"].append(
        {"id": "consulting", "points": 1, "criterion": "Client-facing consulting delivery"})
    assert prompt_template.overlaps(profile)["scored_twice"] == []


def test_the_staffing_sentence_needs_staffing_in_the_profile():
    profile = _profile()
    assert "The staffing/HR services -1 applies only" in _prompt(profile)
    profile["industries"]["deprioritize"] = [e for e in profile["industries"]["deprioritize"] if "staffing" not in e]
    text = _prompt(profile)
    assert "staffing/HR services -1" not in text
    assert "not on the firm that posted the role.\nWhen the posting company is the employer" in text


def test_the_example_profile_carries_neither_sentence():
    text = _prompt(_profile(ROOT / "profile.example.yaml"))
    assert "as in SCORING above" not in text and "staffing/HR services -1" not in text
    assert "crypto.\n" in text


def test_calibrate_counts_api_calls_apart_from_postings():
    cost = {"calls": 10}
    assert calibrate.calls_line(9, cost) == (
        "Claude calls made: 10, including 1 retry after a malformed reply. Postings scored: 9.")
    assert calibrate.calls_line(9, {"calls": 9}) == "Claude calls made: 9. Postings scored: 9."
    assert calibrate.calls_line(3) == "Claude calls made: 3. Postings scored: 3."


def test_the_export_defaults_to_the_public_repository():
    src = (ROOT / "tools" / "export" / "export_public.py").read_text()
    assert 'default="stephareno/jobdigest-open"' in src
