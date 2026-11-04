"""Regression tests for BATCH-05: SRC-16 (wrong company names), FIL-08
(missing relo tag / undetected international Workday postings), DEL-08
(sub-7 rows in the "score 7+" section)."""

import csv
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import emailer
import scorer
from location import needs_relo, location_penalty, is_international
from sources.watchlist import _workday_location
from sourcing.merge_ats_resolved import main as merge_main


# --- FIL-08: relo/location_penalty recomputed after scoring corrects work_model ---

def test_relo_recomputed_after_scorer_corrects_work_model():
    """Greenhouse-style job: ATS parser gave no work_model (blank), but the
    JD is plainly in-office. Claude corrects work_model during scoring; relo
    and location_penalty must reflect that corrected value, not the stale
    pre-scoring one — the exact bug that hid the relo tag for Star Catcher,
    Enovis, and BillionToOne."""
    job = {
        "title": "Director, IT Operations", "company": "Star Catcher",
        "location": "Jacksonville, FL", "work_model": "",
        "description": "Five days a week in our Jacksonville office.",
    }
    job["relo"] = needs_relo(job)
    job["location_penalty"] = location_penalty(job)
    assert job["relo"] is False  # wrong, but this is the pre-scoring state

    fake_result = {"score": 7, "rationale": "x", "is_insurance": False,
                    "is_cool_company_wrong_role": False, "work_model": "In-office"}
    with patch.object(scorer, "_score_single", return_value=fake_result):
        scored = scorer.score_jobs([job])

    assert scored[0]["relo"] is True
    # Jacksonville is outside the would-relocate list → fall-through tier (FIL-09)
    assert scored[0]["location_penalty"] == -5
    assert scored[0]["score"] == 2  # 7 - 5, penalty actually applied


def test_relo_stays_false_when_work_model_confirmed_remote():
    job = {"title": "Director, IT", "company": "Acme", "location": "Austin, TX",
           "work_model": ""}
    fake_result = {"score": 8, "rationale": "x", "is_insurance": False,
                    "is_cool_company_wrong_role": False, "work_model": "Remote"}
    with patch.object(scorer, "_score_single", return_value=fake_result):
        scored = scorer.score_jobs([job])
    assert scored[0]["relo"] is False
    assert scored[0]["score"] == 8


# --- FIL-08: Workday's bare facility code needs the country attached ---------

def test_workday_location_appends_non_us_country():
    info = {"location": "DUB11", "country": {"descriptor": "Ireland"}}
    location = _workday_location(info)
    assert location == "DUB11, Ireland"
    assert is_international(location.lower())


def test_bare_dublin_facility_code_is_international():
    assert is_international("dub11")
    assert is_international("dub")
    assert not is_international("dub11, va")
    assert not is_international("dubuque, ia")


def test_workday_location_does_not_clutter_domestic_postings():
    info = {"location": "Denver, Colorado", "country": {"descriptor": "United States"}}
    assert _workday_location(info) == "Denver, Colorado"


def test_workday_location_skips_country_already_named():
    info = {"location": "Dublin, Ireland", "country": {"descriptor": "Ireland"}}
    assert _workday_location(info) == "Dublin, Ireland"


# --- DEL-08: "main" section no longer claims a score floor it doesn't keep ---

def test_main_section_header_does_not_claim_score_floor():
    sections = {
        "main": [{"title": "T", "company": "C", "score": 5, "apply_link": "https://x",
                  "source": "Watchlist"}],
        "worth_a_look": [], "cool_wrong_role": [],
    }
    html = emailer._build_html(sections["main"], sections["worth_a_look"],
                                sections["cool_wrong_role"], [], False, False)
    assert "score 7+" not in html
    assert "Top matches" in html
    assert ">5/10<" in html  # the row's own score badge is still shown


# --- SRC-16: merge guard rejects a careers_page hit on an already-claimed board ---

def _write_csv(path, columns, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def test_merge_guard_rejects_collision(tmp_path, capsys, monkeypatch):
    from sourcing.merge_ats_resolved import OUTPUT_COLUMNS, UNRESOLVED_COLUMNS

    resolved = tmp_path / "resolved.csv"
    unresolved = tmp_path / "unresolved.csv"
    new_resolved = tmp_path / "new_resolved.csv"
    new_unresolved = tmp_path / "new_unresolved.csv"

    _write_csv(resolved, OUTPUT_COLUMNS, [
        {"company": "Axon", "crunchbase_slug": "axon", "category": "x", "hq": "x", "market": "x",
         "ats": "greenhouse", "board_token": "axon", "board_url": "https://x", "method": "slug_guess",
         "careers_url": "", "has_open_jobs": "Y"},
    ])
    _write_csv(unresolved, UNRESOLVED_COLUMNS, [])
    _write_csv(new_resolved, OUTPUT_COLUMNS, [
        {"company": "Prepared", "crunchbase_slug": "prepared", "category": "x", "hq": "x", "market": "x",
         "ats": "greenhouse", "board_token": "axon", "board_url": "https://x", "method": "careers_page",
         "careers_url": "https://prepared.com/careers", "has_open_jobs": "Y"},
        {"company": "GenuinelyNew", "crunchbase_slug": "gn", "category": "x", "hq": "x", "market": "x",
         "ats": "ashby", "board_token": "gn-board", "board_url": "https://y", "method": "careers_page",
         "careers_url": "", "has_open_jobs": "Y"},
    ])
    _write_csv(new_unresolved, UNRESOLVED_COLUMNS, [])

    monkeypatch.setattr(sys, "argv", [
        "merge_ats_resolved.py",
        "--new-resolved", str(new_resolved), "--new-unresolved", str(new_unresolved),
        "--resolved", str(resolved), "--unresolved", str(unresolved),
    ])
    merge_main()

    with resolved.open(newline="") as fh:
        out_rows = {r["company"]: r for r in csv.DictReader(fh)}
    with unresolved.open(newline="") as fh:
        un_rows = {r["company"]: r for r in csv.DictReader(fh)}

    assert out_rows["Axon"]["board_token"] == "axon"
    assert out_rows["Prepared"]["ats"] == ""  # rejected, not merged in
    assert out_rows["GenuinelyNew"]["board_token"] == "gn-board"  # untouched

    assert un_rows["Prepared"]["reason"] == "board_claimed_by_other_company"
    assert "axon" in un_rows["Prepared"]["rejected_board"]
    assert "GenuinelyNew" not in un_rows
