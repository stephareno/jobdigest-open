"""PUB-02: the scoring prompt and the weekly summary prompt are rendered from
profile.yaml through prompts/*.txt.

Covers: the rendered prompts match their golden files
(whitespace aside); the rendered prompts match the golden files byte for byte,
so any wording change shows up as a golden-file diff; the declined-roles block
and pronouns come from the profile; the renderer fails loudly on a hole.
No network access.
"""

import copy
import difflib
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import profile_loader  # noqa: E402
import prompt_template  # noqa: E402
import scorer  # noqa: E402
import weekly_summary  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"
# The weekly prompt's fixture is the f-string source, so it reads "{listing}".
LISTING = "{listing}"


def normalize(s):
    return " ".join(s.split())


def _rendered():
    profile = profile_loader.load()
    return {
        "scoring_prompt": scorer.build_system_prompt(profile),
        "weekly_summary_prompt": weekly_summary.declines_prompt(profile, LISTING),
    }


def _assert_same(name, rendered, expected, label):
    if rendered == expected:
        return
    # Word by word, so the diff points at the words that differ.
    diff = "\n".join(difflib.unified_diff(expected.split(" "), rendered.split(" "),
                                          f"{label} (expected)", f"{name} (rendered)", lineterm=""))
    pytest.fail(f"{name} differs from {label}:\n{diff}")


# --- Golden snapshot -----------------------------------------------------------


@pytest.mark.parametrize("name", ["scoring_prompt", "weekly_summary_prompt"])
def test_rendered_prompt_matches_golden_file(name):
    path = GOLDEN / f"{name}.txt"
    rendered = _rendered()[name]
    if UPDATE_GOLDEN:
        path.write_text(rendered)
    expected = path.read_text()
    if rendered != expected:
        diff = "\n".join(difflib.unified_diff(expected.splitlines(), rendered.splitlines(),
                                              f"tests/golden/{name}.txt", f"{name} (rendered)",
                                              lineterm=""))
        pytest.fail(f"{name} no longer matches tests/golden/{name}.txt. If the change is "
                    f"deliberate, regenerate with: UPDATE_GOLDEN=1 pytest tests/test_scorer_prompt.py\n{diff}")


def test_scorer_system_prompt_is_the_rendered_profile():
    assert scorer.SYSTEM_PROMPT == scorer.build_system_prompt(profile_loader.load())


# --- Values from the profile ---------------------------------------------------


def _profile(**candidate):
    data = copy.deepcopy(profile_loader.read())
    data["candidate"].update(candidate)
    assert profile_loader.validate(data) == []
    return profile_loader._freeze(data)


def test_declined_feedback_names_the_candidate(monkeypatch):
    monkeypatch.setattr(scorer, "PROFILE", _profile(short_name="Alex"))
    block = scorer._format_feedback([{"company": "Acme", "title": "VP IT", "notes": "too far"}])
    assert block.startswith("DECLINED ROLES — Alex passed on these;")


def test_weekly_prompt_names_the_candidate():
    prompt = weekly_summary.declines_prompt(_profile(display_name="Alex Rivera", descriptor="a CIO"),
                                            "- VP IT at Acme: too far")
    assert prompt.startswith("Alex Rivera, a CIO, passed on these roles")
    assert "- VP IT at Acme: too far" in prompt


@pytest.mark.parametrize("pronouns, words", [
    ("she/her", ["Companies she admires", "fit her.", "— her LOWEST"]),
    ("he/him", ["Companies he admires", "fit him.", "— his LOWEST"]),
    ("they/them", ["Companies they admire", "fit them.", "— their LOWEST"]),
])
def test_pronouns_render_from_the_profile(pronouns, words):
    prompt = normalize(scorer.build_system_prompt(_profile(pronouns=pronouns)))
    for w in words:
        assert w in prompt, w


def test_unsupported_pronouns_are_rejected():
    data = copy.deepcopy(profile_loader.read())
    data["candidate"]["pronouns"] = "xe/xem"
    errs = [e for e in profile_loader.validate(data) if e.startswith("candidate.pronouns:")]
    assert errs and "she/her, he/him, they/them" in errs[0]


def test_optional_sections_drop_out_when_empty():
    data = copy.deepcopy(profile_loader.read())
    data["company_fit"]["exemplars"] = []
    data["company_fit"]["size_matters"] = True
    prompt = scorer.build_system_prompt(profile_loader._freeze(data))
    assert "as exemplars" not in prompt
    assert "Company size is NOT a factor" not in prompt


# --- The renderer --------------------------------------------------------------


def test_renderer_values_filters_and_blocks():
    ctx = {"a": {"n": -2, "items": ("x", "y"), "flag": False, "word": "hi"}}
    text = ('{{a.n|signed}} {{a.items|join:" · "}} {{a.word|capfirst}}'
            '{{#if a.flag}} on{{/if}}{{#if not a.flag}} off{{/if}} {"json": 1}')
    assert prompt_template.render(text, ctx) == '-2 x · y Hi off {"json": 1}'


@pytest.mark.parametrize("text, message", [
    ("{{a.missing}}", "no value"),
    ("{{a.items}}", "render it with a join"),
    ("{{a.word|shout}}", "unknown filter"),
    ("{{ a.word }}", "placeholder not rendered"),
])
def test_renderer_fails_loudly(text, message):
    ctx = {"a": {"items": ("x",), "word": "hi"}}
    with pytest.raises(prompt_template.TemplateError, match=message):
        prompt_template.render(text, ctx)
