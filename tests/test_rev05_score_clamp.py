"""REV-05: the model's base score is checked at parse time.

A score must be an integer from 0 to scoring.max_score. 10.0 reads as 10.
47, -3, "8", true, null, a missing score and 7.5 are malformed replies:
each is asked once more, and a second bad reply is recorded as a scoring
error (score 0, "Scoring error") with the raw value in the log, never with
the bad number and never clamped. A normal 7 is unchanged. No network access.
"""

import json

import pytest

import llm
import profile_loader
import scorer

JOB = {"title": "Director of IT", "company": "Acme", "location": "Remote, US",
       "work_model": "Remote", "description": "Leads IT."}
TOP = profile_loader.active()["scoring"]["max_score"]


def _reply(score, **extra):
    body = {"score": score, "rationale": {"company": "c", "role": "r", "culture": "no signal in posting."},
            "criteria_hits": [], "hard_block_industry": None, "is_cool_company_wrong_role": False,
            "function_fit": "leads_department", "work_model": "Remote", "city": None, "state": None,
            "hiring_company": "Acme", "website": None, **extra}
    if score is _MISSING:
        del body["score"]
    return json.dumps(body)


_MISSING = object()


def test_max_score_is_ten_on_this_profile():
    assert TOP == 10                                   # the cases below are written against 10


@pytest.mark.parametrize("bad, shown", [
    (47, "47"), (-3, "-3"), ("8", "'8'"), (True, "True"), (None, "None"),
    (_MISSING, "None"), (7.5, "7.5"), (11, "11"), ("ten", "'ten'"), ([8], "[8]"),
])
def test_a_bad_score_is_retried_once_then_recorded_as_a_scoring_error(llm_stub, capsys, bad, shown):
    stub = llm_stub([_reply(bad), _reply(bad)])
    result = scorer._score_single(dict(JOB))
    assert len(stub.calls) == 2                                          # one retry
    assert result["score"] == 0 and result["rationale"] == scorer.SCORING_ERROR
    assert "scoring_error" not in result                                 # recorded, not deferred
    out = capsys.readouterr().out
    reason = f"score {shown} is not an integer from 0 to {TOP}"
    assert f"[LLM] scoring call: malformed JSON reply ({reason}), asking once more" in out
    assert f"[Scorer] Error: {reason} — recorded as 'Scoring error' (Acme / Director of IT)" in out


@pytest.mark.parametrize("bad", [47, -3, "8", True, None])
def test_a_bad_score_then_a_good_one_uses_the_good_one(llm_stub, bad):
    stub = llm_stub([_reply(bad), _reply(6)])
    result = scorer._score_single(dict(JOB))
    assert len(stub.calls) == 2 and result["score"] == 6
    assert result["rationale"] != scorer.SCORING_ERROR


def test_ten_point_zero_is_accepted_as_ten(llm_stub):
    stub = llm_stub([_reply(10.0)])
    result = scorer._score_single(dict(JOB))
    assert len(stub.calls) == 1
    assert result["score"] == 10 and type(result["score"]) is int


@pytest.mark.parametrize("good", [0, 7, 10])
def test_an_in_range_integer_is_unchanged(llm_stub, good):
    stub = llm_stub([_reply(good)])
    result = scorer._score_single(dict(JOB))
    assert len(stub.calls) == 1 and result["score"] == good and type(result["score"]) is int


def test_never_clamped():
    with pytest.raises(ValueError, match="score 99 is not an integer from 0 to 10"):
        scorer.checked_score({"score": 99}, max_score=10)
    assert scorer.checked_score({"score": 9.0, "x": 1}, max_score=10) == {"score": 9, "x": 1}


def test_the_range_follows_scoring_max_score():
    assert scorer.checked_score({"score": 12}, max_score=12)["score"] == 12
    with pytest.raises(ValueError):
        scorer.checked_score({"score": 11}, max_score=10)


def test_a_bad_score_through_score_jobs_lands_as_a_disqualified_scoring_error(llm_stub, monkeypatch):
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    llm_stub([_reply(99), _reply(99)])
    [job] = scorer.score_jobs([dict(JOB, apply_link="https://example.com/1")])
    assert job["score"] == 0 and job["rationale"] == scorer.SCORING_ERROR
    assert scorer.filter_jobs([job])["main"] == []                       # never at the top of the digest


def test_score_is_the_only_number_in_the_reply_schema():
    """Item 2: nothing else the reply carries is added to the score. The
    modifiers (location, pay, work model, criteria points) come from code and
    the profile."""
    import re
    from pathlib import Path
    prompt = (Path(scorer.__file__).parent / "prompts" / "scoring_system.txt").read_text()
    schema = prompt[prompt.index("Return ONLY this JSON:"):]
    assert re.findall(r'"(\w+)": <integer', schema) == ["score"]
    assert "<number" not in schema


def test_other_json_callers_are_unchanged(llm_stub):
    llm_stub(['{"a": 1}'])
    obj, _ = llm.complete_json("m", None, [{"role": "user", "content": "x"}], 10)
    assert obj == {"a": 1}
