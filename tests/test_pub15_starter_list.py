"""PUB-15: one starter list (starter_companies.csv, segments public and
startup) with one boards file (starter_boards.csv), the segment filter, the
three build tools that share them, and the export's OWNER/REPO placeholder."""

import csv
import sys
from collections import Counter
from pathlib import Path

import pytest

import company_lists
from tools import build_public_boards as bpb
from tools import build_starter_boards as bsb
from tools import build_starter_companies as starter

ROOT = Path(__file__).resolve().parent.parent
LISTS = ROOT / "data" / "company_lists"
sys.path.insert(0, str(ROOT / "tools" / "export"))

import export_public as ep  # noqa: E402

HEADER = "name,website,notes,segment,ticker,sic,sic_description,state,outcome\n"


def _write(path, text):
    path.write_text(text)
    return str(path)


# --- The segment filter ------------------------------------------------------------------

ROWS = [{"name": "Chip Co", "segment": "public", "sic": "3674", "state": "CA"},
        {"name": "Bank Co", "segment": "public", "sic": "6022", "state": "VA"},
        {"name": "Seed Co", "segment": "startup", "sic": "", "state": ""}]


@pytest.mark.parametrize("flt,kept", [
    ({"segment": ["public"]}, ["Chip Co", "Bank Co"]),
    ({"segment": ["startup"]}, ["Seed Co"]),
    ({"segment": ["public", "startup"], "sic": ["3670-3679"]}, ["Chip Co", "Seed Co"]),   # sic narrows public only
    ({"sic": ["6022"]}, ["Bank Co", "Seed Co"]),
    ({"states": ["CA"]}, ["Chip Co"]),                                                   # states tests every row
])
def test_segment_filter_and_sic_narrows_the_public_segment_only(flt, kept):
    assert [r["name"] for r in ROWS if company_lists.matches(r, flt)] == kept


def test_a_row_with_no_segment_fails_a_segment_filter():
    assert not company_lists.matches({"name": "Old Co"}, {"segment": ["public"]})


def test_segment_values_are_validated(tmp_path):
    lst = _write(tmp_path / "l.csv", HEADER)
    errors = []
    company_lists.check_entries([{"file": lst, "tier": "watch", "filter": {"segment": ["private"]}}], "x", errors)
    assert errors == ["x[0].filter.segment: ['private'] — each one of public, startup"]


# --- The shipped files -------------------------------------------------------------------

def test_the_old_lists_are_gone():
    assert not (LISTS / "public_companies_us.csv").exists() and not (LISTS / "public_boards.csv").exists()


def test_starter_companies_segments_and_outcomes():
    rows = company_lists.read_list(LISTS / "starter_companies.csv")
    by_segment = Counter(r["segment"] for r in rows)
    assert by_segment["public"] > 4000 and by_segment["startup"] > 5000 and set(by_segment) == {"public", "startup"}
    startups = [r for r in rows if r["segment"] == "startup"]
    assert all(not (r["ticker"] or r["sic"] or r["sic_description"] or r["state"]) for r in startups)
    assert {r["outcome"] for r in startups} <= {"board", "workday"}


def test_starter_boards_writes_a_shared_board_once():
    keys = [starter.board_key(b) for b in company_lists.read_list(LISTS / "starter_boards.csv")]
    assert len(keys) == len(set(keys))


# --- The build tools ---------------------------------------------------------------------

def test_build_starter_companies_keeps_startups_and_carries_outcomes(tmp_path):
    out = tmp_path / "starter.csv"
    out.write_text("# old\n" + HEADER + "Kept Co,,,public,KC,7372,Software,MD,careers_page\n"
                   "Seed Co,,,startup,,,,,board\n")
    built = _write(tmp_path / "built.csv", "name,website,website_source,cik,ticker,exchange,sic,sic_description,"
                   "state,outcome,notes\nKEPT CO,https://kept.example,wikidata,1,KC,Nasdaq,7372,Software,MD,,\n"
                   "New Co,,,2,NC,NYSE,3812,Search,VA,,\n")
    starter.main(["--public", built, "--out", str(out)])
    lines = out.read_text().splitlines()
    assert lines[0].startswith("# Built ") and lines[1] == HEADER.strip()
    rows = company_lists.read_list(str(out))
    assert [(r["name"], r["segment"], r["outcome"]) for r in rows] == [
        ("KEPT CO", "public", "careers_page"), ("New Co", "public", ""), ("Seed Co", "startup", "board")]
    assert "website_source" not in rows[0] and rows[0]["website"] == "https://kept.example"


def test_build_starter_boards_keeps_the_public_rows_and_drops_their_repeats():
    built = [{"name": "Seed Co", "ats": "lever", "url": "https://api.lever.co/v0/postings/seed", "website": ""},
             {"name": "Both Co", "ats": "lever", "url": "https://api.lever.co/v0/postings/both", "website": ""}]
    existing = [{"name": "Old Seed", "ats": "lever", "url": "https://api.lever.co/v0/postings/old", "website": ""},
                {"name": "BOTH CO", "ats": "lever", "url": "https://api.lever.co/v0/postings/both", "website": "x"},
                {"name": "Pub Co", "ats": "careers_page", "url": "https://pub.example/jobs", "website": ""}]
    companies = [{"name": "BOTH CO", "segment": "public"}, {"name": "Pub Co", "segment": "public"},
                 {"name": "Seed Co", "segment": "startup"}, {"name": "Both Co", "segment": "startup"}]
    assert [b["name"] for b in bsb.merged_boards(built, existing, companies)] == ["Seed Co", "Both Co", "Pub Co"]
    rows = bsb.startup_companies(built + [{**built[0], "ats": "workday"}], companies)
    assert [(r["name"], r["segment"], r["outcome"]) for r in rows] == [("Seed Co", "startup", "board"),
                                                                       ("Both Co", "startup", "board")]


def test_build_public_boards_replaces_only_the_public_rows(tmp_path):
    companies = tmp_path / "starter.csv"
    companies.write_text(HEADER + "Pub Co,,,public,,,,,\nSeed Co,,,startup,,,,,board\n")
    boards = tmp_path / "boards.csv"
    boards.write_text("name,ats,url,website\nSeed Co,lever,https://api.lever.co/v0/postings/seed,\n"
                      "Pub Co,lever,https://api.lever.co/v0/postings/stale,\n")
    bpb.write_boards([{"name": "Pub Co", "ats": "lever", "url": "https://api.lever.co/v0/postings/pub", "website": ""},
                      {"name": "SEED CO", "ats": "lever", "url": "https://api.lever.co/v0/postings/seed", "website": ""}],
                     boards, companies)
    assert [(r["name"], r["url"].rsplit("/", 1)[1]) for r in company_lists.read_list(str(boards))] == [
        ("Seed Co", "seed"), ("Pub Co", "pub")]
    rows = bpb.update_list({"pub co": "board", "seed co": "none"}, {}, companies, mark_untried=True)
    assert [(r["name"], r["outcome"]) for r in rows] == [("Pub Co", "board"), ("Seed Co", "board")]
    assert companies.read_text().splitlines()[0] == HEADER.strip()      # no website_source column added


def test_check_guessed_needs_a_list_with_website_source(tmp_path):
    lst = _write(tmp_path / "l.csv", HEADER + "Pub Co,,,public,,,,,\n")
    with pytest.raises(SystemExit, match="no website_source column"):
        bpb.check_guessed(lst, boards_path=lst)


def test_the_monthly_retry_reads_the_whole_starter_list():
    text = (ROOT / ".github" / "workflows" / "resolve_ats.yml").read_text()
    assert "public_companies_us" not in text and "public_boards" not in text
    assert "JOB_DIGEST_PROFILE=/tmp/retry_profile.yaml" in text
    assert "--list data/company_lists/starter_companies.csv" in text


# --- Export: the OWNER/REPO placeholder --------------------------------------------------

def test_repo_fills_every_placeholder(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_text('<a href="https://github.com/OWNER/REPO/blob/main/x">\n'
                                                  "OWNER/REPO and OWNER/REPO\n")
    (tmp_path / "README.md").write_text("no placeholder\n")
    (tmp_path / "tool.py").write_text('PLACEHOLDER = "OWNER/REPO"\n')      # code naming it stays as it is
    assert ep.placeholder_hits(tmp_path) == ["docs/index.html:1", "docs/index.html:2"]
    assert ep.fill_repo(tmp_path, "someone/job-digest-agent") == 3
    assert ep.placeholder_hits(tmp_path) == []
    assert (tmp_path / "tool.py").read_text() == 'PLACEHOLDER = "OWNER/REPO"\n'
    assert "github.com/someone/job-digest-agent/blob" in (tmp_path / "docs" / "index.html").read_text()


def test_the_check_fails_when_a_placeholder_survives_repo(tmp_path):
    (tmp_path / "late.md").write_text("see OWNER/REPO\n")
    assert ep.check_placeholders(tmp_path, "someone/job-digest-agent") == [
        "1 OWNER/REPO placeholder(s) survived --repo someone/job-digest-agent"]
    assert ep.check_placeholders(tmp_path, None) == []          # without --repo they are expected
    ep.fill_repo(tmp_path, "someone/job-digest-agent")
    assert ep.check_placeholders(tmp_path, "someone/job-digest-agent") == []


def test_the_check_runs_the_placeholder_test_before_the_scrub():
    source = Path(ep.__file__).read_text()
    assert source.index("failures += check_placeholders(out, args.repo)") < source.index("hits = scrub(out, items")


def test_the_setup_guide_may_name_the_linkedin_label(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "SETUP_GUIDE.md").write_text("for example `my-label`\n")
    (tmp_path / "docs" / "PRD.md").write_text("for example `my-label`\n")
    assert ep.scrub(tmp_path, [("LinkedIn label", "my-label", frozenset())]) == ["docs/PRD.md:1: LinkedIn label 'my-label'"]


def test_the_scrub_does_not_count_the_repo_address(tmp_path):
    (tmp_path / "a.md").write_text("https://github.com/maint/job-digest-agent and maint alone\n")
    items = [("repo owner", "maint", frozenset())]
    assert ep.scrub(tmp_path, items) == ["a.md:1: repo owner 'maint'"]
    (tmp_path / "a.md").write_text("https://github.com/maint/job-digest-agent\n")
    assert ep.scrub(tmp_path, items, ("maint/job-digest-agent",)) == []


@pytest.mark.parametrize("value", ["OWNER", "a/b/c", "-bad/name", ""])
def test_repo_must_be_owner_slash_name(value, tmp_path):
    with pytest.raises(SystemExit):
        ep.main(["--out", str(tmp_path / "o"), "--repo", value])
