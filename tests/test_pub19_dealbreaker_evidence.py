"""PUB-19: a dealbreaker applies only on evidence the code can verify.

Covers: a reported dealbreaker with a quote that is in the posting, at least
20 characters and holding an evidence term scores 0 with its reason; a quote
not in the posting, one with no evidence term, and one under 20 characters
are each ignored, recorded in dealbreaker_unverified and logged; a posting
that plainly says "individual contributor role, no direct reports" scores 0;
validation requires evidence_terms on a dealbreaker and refuses them on a
points criterion; the reply's dealbreaker_evidence keeps dealbreaker ids
only; calibrate's drivers block names an ignored dealbreaker; a profile with
no dealbreakers asks for no evidence.
"""

import json
from pathlib import Path

import pytest

import calibrate
import llm
import location
import profile_loader
import prompt_template
import scorer
from tests.llm_stub import StubProvider

ROOT = Path(__file__).resolve().parent.parent
NO_TEAM = {"id": "no_team", "dealbreaker": True, "criterion": "Individual contributor role with no team",
           "evidence_terms": ["individual contributor", "no direct reports", "no team", "sole", "without a team"]}
TEAM_OF_TWO = ("We are hiring an FP&A Director to lead planning. You will report to the Chief Financial "
               "Officer and lead a team of two analysts. This role is onsite five days a week.")


@pytest.fixture
def with_no_team(monkeypatch):
    raw = profile_loader.read()
    raw["scoring"]["role_criteria"].append(NO_TEAM)
    profile = profile_loader.with_defaults(raw)
    monkeypatch.setattr(location, "CONFIG", location.build_config(profile))
    monkeypatch.setattr(scorer, "PROFILE", profile)
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    return profile


def _scored(monkeypatch, description, evidence, hits=("no_team",), score=8):
    payload = {"score": score, "criteria_hits": list(hits), "dealbreaker_evidence": evidence,
               "rationale": {"company": "Restaurant chain.", "role": "Top FP&A seat.", "culture": "candid"},
               "hard_block_industry": None, "is_cool_company_wrong_role": False,
               "function_fit": "leads_department", "work_model": "Remote", "city": None, "state": None,
               "hiring_company": "Acme", "website": "acme.com"}
    monkeypatch.setattr(llm, "_provider", StubProvider(lambda call: json.dumps(payload)))
    job = {"title": "FP&A Director", "company": "Acme", "location": "Remote, US", "work_model": "Remote",
           "description": description, "comp": "Pay range: $250,000 - $300,000"}
    scorer.score_jobs([job])
    return job


def _ignored(job, reason, capsys):
    assert job["score"] > 0 and not job.get("dealbreaker") and not scorer.is_disqualified(job)
    assert [(m["id"], m["reason"]) for m in job["dealbreaker_unverified"]] == [("no_team", reason)]
    assert (f"[Scorer] dealbreaker no_team reported without verifiable evidence; ignored: {reason}"
            in capsys.readouterr().out)
    assert "no_team" in job["criteria_hits"]            # the report itself is kept


def test_a_verified_dealbreaker_scores_zero_with_its_reason(with_no_team, monkeypatch, capsys):
    sentence = "This is an individual contributor role with no direct reports."
    job = _scored(monkeypatch, f"We are hiring an FP&A Director.\n\n{sentence}\nRemote.",
                  {"no_team": "this is an Individual contributor   role with no direct reports."})
    assert job["score"] == 0 and job["dealbreaker"] == "no team" and job["dealbreaker_criterion"] == "no_team"
    assert location.split_rationale(job["rationale"])[1] == "Dealbreaker: no team."
    assert scorer.is_disqualified(job) and "dealbreaker_unverified" not in job
    assert "without verifiable evidence" not in capsys.readouterr().out


def test_a_quote_not_in_the_posting_is_ignored(with_no_team, monkeypatch, capsys):
    job = _scored(monkeypatch, TEAM_OF_TWO, {"no_team": "This is an individual contributor role with no team."})
    _ignored(job, "quote not found in the posting", capsys)
    assert job["dealbreaker_unverified"][0]["quote"] == "This is an individual contributor role with no team."


def test_a_quote_without_an_evidence_term_is_ignored(with_no_team, monkeypatch, capsys):
    # The Los Pollos Hermanos case: the quote is real, but leading two analysts is a team.
    job = _scored(monkeypatch, TEAM_OF_TWO,
                  {"no_team": "You will report to the Chief Financial Officer and lead a team of two analysts."})
    _ignored(job, "quote has none of the evidence terms", capsys)


def test_a_quote_under_twenty_characters_is_ignored(with_no_team, monkeypatch, capsys):
    job = _scored(monkeypatch, "Sole contributor. No team. Remote.", {"no_team": "No team."})
    _ignored(job, "quote under 20 characters", capsys)


def test_no_quote_at_all_is_ignored(with_no_team, monkeypatch, capsys):
    job = _scored(monkeypatch, TEAM_OF_TWO, {})
    _ignored(job, "no sentence quoted", capsys)


def test_a_posting_that_says_it_plainly_scores_zero(with_no_team, monkeypatch):
    text = ("Director, FP&A Modeling. This is an individual contributor role, no direct reports. "
            "You will build the company's planning models.")
    job = _scored(monkeypatch, text, {"no_team": "This is an individual contributor role, no direct reports."})
    assert job["score"] == 0 and job["dealbreaker"] == "no team"


def test_an_evidence_term_matches_whole_words_only(with_no_team):
    job = {"description": "Our consoles ship worldwide and our consoles team is large.",
           "dealbreaker_evidence": {"no_team": "Our consoles ship worldwide and our consoles team is large."}}
    assert location.verify_dealbreaker(job, "no_team") == (False, "quote has none of the evidence terms")


def test_points_criteria_are_untouched(with_no_team, monkeypatch):
    first = profile_loader.read()["scoring"]["role_criteria"][0]["id"]
    job = _scored(monkeypatch, TEAM_OF_TWO, {}, hits=(first,), score=8)
    assert job["score"] > 0 and "dealbreaker_unverified" not in job and job["criteria_hits"] == [first]


# --- validation and parsing ---------------------------------------------------------------

def _criteria_errors(entry):
    raw = profile_loader.read()
    raw["scoring"]["role_criteria"].append(entry)
    return [e for e in profile_loader.validate(raw) if "role_criteria" in e]


@pytest.mark.parametrize("terms", [None, [], [""], "no team"])
def test_a_dealbreaker_needs_evidence_terms(terms):
    entry = {k: v for k, v in NO_TEAM.items() if k != "evidence_terms"}
    if terms is not None:
        entry["evidence_terms"] = terms
    errors = _criteria_errors(entry)
    assert len(errors) == 1 and "evidence_terms: required for a dealbreaker" in errors[0]
    assert _criteria_errors(NO_TEAM) == []


def test_a_points_criterion_takes_no_evidence_terms():
    errors = _criteria_errors({"id": "x", "points": 2, "criterion": "y", "evidence_terms": ["z"]})
    assert len(errors) == 1 and errors[0].endswith(".evidence_terms: only a dealbreaker takes evidence_terms")


def test_the_reply_keeps_evidence_for_dealbreakers_only():
    criteria = [{"id": "team", "points": 1, "criterion": "x"}, NO_TEAM]
    got = scorer.dealbreaker_evidence({"dealbreaker_evidence": {"NO_TEAM": "  a   sentence ", "team": "x",
                                                                "other": "y"}}, criteria)
    assert got == {"no_team": "a sentence"}
    assert scorer.dealbreaker_evidence({"dealbreaker_evidence": "no team"}, criteria) == {}
    assert scorer.dealbreaker_evidence({}, criteria) == {}


def test_calibrate_names_an_ignored_dealbreaker(with_no_team):
    job = {"criteria_hits": ["no_team"], "base_score": 8,
           "dealbreaker_unverified": [{"id": "no_team", "reason": "quote has none of the evidence terms",
                                       "quote": "Lead a team of two analysts."}]}
    lines = calibrate.drivers(job, with_no_team)
    assert ("scoring.role_criteria.no_team: dealbreaker reported without verifiable evidence; ignored: "
            'quote has none of the evidence terms (quoted: "Lead a team of two analysts.")') in lines


def test_a_profile_without_dealbreakers_asks_for_no_evidence():
    raw = profile_loader.read()
    text = prompt_template.render((ROOT / "prompts" / "scoring_system.txt").read_text(),
                                  prompt_template.context(raw))
    assert "dealbreaker_evidence" not in text and "DEALBREAKERS" not in text


def test_the_example_profile_lists_its_evidence_terms():
    example = profile_loader.read(str(ROOT / "profile.example.yaml"))
    deal = next(c for c in example["scoring"]["role_criteria"] if c.get("dealbreaker"))
    assert deal["evidence_terms"] == NO_TEAM["evidence_terms"]


def test_the_interview_asks_for_a_new_dealbreakers_evidence_terms():
    import setup_interview

    class UI:
        def __init__(self):
            self.asked = []

        def say(self, text):
            pass

        def lines(self, question, default):
            return ["No budget authority"] if question.startswith("Dealbreakers") else list(default)

        def text(self, question, default, check=None):
            return str(default)

        def items(self, question, default, check=None, allow_empty=True, metros=False):
            self.asked.append((question, allow_empty))
            return ["no budget", "budget owned elsewhere"]

    ui = UI()
    current = [{"id": "team", "points": 1, "criterion": "Manages a team"}, NO_TEAM]
    out = setup_interview.role_criteria(ui, current)
    assert NO_TEAM not in out or out[out.index(NO_TEAM)] == NO_TEAM   # an entry kept keeps its terms
    new = next(c for c in out if c["criterion"] == "No budget authority")
    assert new["dealbreaker"] is True and new["evidence_terms"] == ["no budget", "budget owned elsewhere"]
    assert ui.asked == [('Words a posting would use to say "No budget authority" (e.g. no direct reports)', False)]
