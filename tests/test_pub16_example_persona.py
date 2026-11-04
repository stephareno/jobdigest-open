"""PUB-16: the example persona (a Director of FP&A in Chicago) and the dream tag.

Covers: the example profile shares no pay threshold, exemplar, dream company,
criterion text, target title or industry tier entry with the committed
profile (skipped where profile.yaml is absent, as in the public export); the
twelve sample postings rank in the order their cases imply under stubbed base
scores; the digest's "dream" pill and the Sheet's "Dream" column.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "profile.example.yaml"
COMMITTED = ROOT / "profile.yaml"

# The tier lists that describe a candidate's taste. industries.hard_block and
# vice_core_business are left out: they are a common blocklist (gambling,
# tobacco, alcohol), the same for many job seekers, and reveal nobody.
INDUSTRY_TIERS = ("deprioritize", "lowest_interest", "neutral", "lean_in", "strong_yes", "sector_order")


def _fold(values):
    return {" ".join(str(v).split()).casefold() for v in values}


def _identifying(profile, dream_file):
    comp = profile["compensation"]
    usajobs_floor = profile["sources"]["usajobs"].get("pay_floor_from")
    dreams = [line.strip() for line in dream_file.read_text().splitlines() if line.strip()] \
        if dream_file.exists() else []
    return {
        "pay threshold": {comp["floor"], comp["stretch"]} | ({usajobs_floor} if isinstance(usajobs_floor, int) else set()),
        "exemplar": _fold(profile["company_fit"]["exemplars"]),
        "dream company": _fold(dreams),
        "criterion text": _fold(c["criterion"] for c in profile["scoring"]["role_criteria"]),
        "target title": _fold(profile["roles"]["target_titles"]),
        "industry entry": _fold(v for tier in INDUSTRY_TIERS for v in profile["industries"].get(tier) or []),
    }


# Every other profile in the checkout: the maintainer's profile.yaml where it
# exists (never in the public export) and the test fixture, so the check runs
# everywhere and never skips.
OTHER_PROFILES = [p for p in (COMMITTED, ROOT / "tests" / "fixtures" / "profile_test.yaml") if p.exists()]


@pytest.mark.parametrize("other", OTHER_PROFILES, ids=lambda p: p.name)
def test_the_example_shares_nothing_identifying_with_another_profile(other):
    example = yaml.safe_load(EXAMPLE.read_text())
    committed = yaml.safe_load(other.read_text())
    mine = _identifying(example, ROOT / example["dream_companies"]["file"])
    theirs = _identifying(committed, ROOT / committed["dream_companies"]["file"])
    theirs["exemplar"] |= theirs["dream company"]
    theirs["dream company"] |= theirs["exemplar"]
    shared = {kind: sorted(map(str, mine[kind] & theirs[kind])) for kind in mine if mine[kind] & theirs[kind]}
    assert shared == {}


def test_the_example_is_a_finance_persona():
    example = yaml.safe_load(EXAMPLE.read_text())
    assert example["roles"]["target_department"].startswith("financial planning and analysis")
    assert example["location"]["home"] == "Chicago, IL"
    assert [c["id"] for c in example["scoring"]["role_criteria"]] == [
        "owns_plan", "cfo_line", "team", "board_reporting", "travel", "no_team"]
    assert example["company_fit"]["exemplars"] == ["Wayne Enterprises", "Stark Industries"]


# --- the twelve samples under the example profile ----------------------------------------

ORDER_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
stub = json.loads(sys.argv[1])
import scorer, calibrate

def fake(job, feedback_block=""):
    base, fit = stub[job["company"]]
    return {"score": base, "rationale": "stub. stub.", "hard_block_industry": None,
            "is_cool_company_wrong_role": False, "work_model": job["work_model"], "loc_city": "",
            "loc_state": "", "hiring_company": "", "website": "", "function_fit": fit}

scorer._score_single = fake
scorer.time.sleep = lambda s: None
ranked, calls = calibrate.run("docs/samples")
print(json.dumps({"order": [j["file"] for j in ranked], "calls": calls,
                  "finals": {j["file"]: j["score"] for j in ranked},
                  "dream": sorted(j["company"] for j in ranked if j.get("is_dream_company")),
                  "relo": sorted(j["company"] for j in ranked if j.get("relo"))}))
"""


def test_the_samples_rank_in_their_cases_order_under_the_example_profile():
    """Each sample's modifiers place it, given the stubbed base scores in
    test_pub06 (01 > 02 > 03 > 06 ≈ 10 > 05 > 04 > 08 > 07 > the three zeros)."""
    import json
    import os
    import subprocess
    import sys
    from tests.test_pub06_setup_commands import EXPECTED_ORDER, STUB
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "GMAIL_"))}
    env.update(JOB_DIGEST_PROFILE=str(EXAMPLE), ANTHROPIC_API_KEY="stub")
    out = subprocess.run([sys.executable, "-c", ORDER_SCRIPT, json.dumps(STUB)], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout.splitlines()[-1]
    got = json.loads(out)
    assert got["order"] == EXPECTED_ORDER
    assert got["calls"] == 9
    assert got["finals"]["01-strong-fit-hybrid-home.md"] == 10
    assert got["finals"]["06-relocate-metro-dream.md"] == got["finals"]["10-staffing-firm-client.md"] == 7
    assert got["finals"]["04-wrong-function.md"] == 4 and got["finals"]["07-elsewhere-onsite.md"] == 2
    assert got["dream"] == ["Pym Technologies", "Stark Industries", "Wayne Enterprises"]
    assert got["relo"] == ["Los Pollos Hermanos", "Pym Technologies"]


# --- the dream tag ---------------------------------------------------------------------

def _card_job(**kw):
    return {"title": "Director of FP&A", "company": "Pym Technologies", "location": "Seattle, WA",
            "work_model": "Hybrid", "comp": "", "score": 8, "rationale": "Materials maker. Top seat.",
            "apply_link": "https://example.com/1", "source": "ats", **kw}


def test_a_dream_company_card_carries_the_dream_pill_beside_the_score():
    import emailer
    dream = emailer._job_card(_card_job(is_dream_company=True, relo=True))
    other = emailer._job_card(_card_job(company="Pied Piper"))
    assert ">dream</span>" in dream and ">dream</span>" not in other
    # beside the score, before the other pills, and not in the relo pill's colours
    assert dream.index(">8/10<") < dream.index(">dream<") < dream.index(">relo<")
    assert "#FBEAF0" in dream and "#FBEAF0" not in emailer._relo_badge({"relo": True})


def test_the_cool_company_card_names_a_dream_company_too():
    import emailer
    job = _card_job(company="Stark Industries", is_dream_company=True, is_cool_company_wrong_role=True,
                    function_fit="outside_department", function_capped=True, score=4)
    html = emailer._build_html([], [], [job], [], False, False)
    assert "Stark Industries" in html and ">dream</span>" in html


def test_the_sheet_writes_dream_only_into_an_existing_column():
    import sheet_layout
    import sheets
    data = sheets.main_row_data(_card_job(is_dream_company=True))
    assert data["Dream"] == "Y" and sheets.main_row_data(_card_job())["Dream"] == ""
    without = [h for h in sheet_layout.SHEET_COLUMNS]
    assert "Dream" not in without and len(sheets._row_values(data, without)) == len(without)
    assert sheets._row_values(data, without + ["Dream"])[-1] == "Y"
    assert "Dream" in sheet_layout.OPTIONAL_COLUMNS            # sheet-init lays it out; the pipeline never adds it
    src = (ROOT / "sheets.py").read_text()
    assert '_ensure_header(sheet, headers, "Dream")' not in src
    assert src.count('"Dream": "Y" if job.get("is_dream_company") else ""') == 4   # main and Disqualified writers


# --- the generated samples ---------------------------------------------------------------

def test_the_page_carries_the_generated_images():
    """docs/index.html embeds exactly the committed PNGs (tools/embed_page_images.py)."""
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import embed_page_images
    page = (ROOT / "docs" / "index.html").read_text()
    assert embed_page_images.build(page) == page


def test_the_saved_results_are_the_twelve_samples_under_the_example_profile():
    import json
    data = json.loads((ROOT / "docs" / "sample_calibrate.json").read_text())
    files = sorted(p.name for p in (ROOT / "docs" / "samples").glob("*.md"))
    assert sorted(j["file"] for j in data["jobs"]) == files and data["postings_scored"] == 9
    assert data["claude_calls"] == 10   # the real run: one retry after a malformed reply
    dream = {j["company"] for j in data["jobs"] if j.get("is_dream_company")}
    assert dream == {"Wayne Enterprises", "Pym Technologies", "Stark Industries"}
    html = (ROOT / "docs" / "sample_digest.html").read_text()
    for company in ("Wayne Enterprises", "Pym Technologies"):
        at = html.index(company)
        card = html[html.rindex('<div style="background:#ffffff', 0, at):at]   # this card, up to the name
        assert ">dream</span>" in card, company
