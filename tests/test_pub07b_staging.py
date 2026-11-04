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


# --- fixes from the stranger test ----------------------------------------------------

def test_builtin_searches_with_search_not_title():
    import config
    assert "?search={query}" in config.BUILTIN_SEARCH_URL and "?title=" not in config.BUILTIN_SEARCH_URL
    for path in ("profile.example.yaml", "tests/fixtures/profile_test.yaml"):
        assert "?title=" not in (ROOT / path).read_text(), path


def test_doctor_imap_passes_on_login_with_no_profile_yet(monkeypatch):
    import imaplib
    import doctor

    class FakeIMAP:
        def __init__(self, *a, **k):
            pass

        def login(self, *a):
            return "OK", []

        def logout(self):
            pass

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv("GMAIL_ADDRESS", "you@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    out = doctor.check_gmail_imap(None)
    assert "PASS" in str(out) and "no profile yet" in str(out)


def test_doctor_sheet_check_needs_no_profile():
    """The Sheet check names its tabs from sheet_layout, never config (which loads a profile)."""
    import inspect
    import doctor
    src = inspect.getsource(doctor.check_google)
    assert "import config" not in src and "sheet_layout.SHEET_NAME" in src


def test_setup_says_which_dream_list_enter_keeps():
    src = (ROOT / "setup_interview.py").read_text()
    assert "Enter keeps the current file" not in src and "the example's names" in src
