"""PUB-07a: the public export tool (tools/export/export_public.py).

Covers: allowlist matching (globs never cross "/", dir/ takes everything
under it, !lines carve exceptions), the never-ship guard, the allowlist file
itself (it ships no state, profile or personal file), detecting test files that
import a module the export lacks, the framework-only ROADMAP, the scrub's
whole-word matching and exemptions, the rewrite table. No network, no git
writes; nothing here exports the repo (the tool's --check does that).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "export"))

import export_public as ep  # noqa: E402

TRACKED = ["main.py", "connections.py", "backfill_linkedin.py", "sources/ats.py",
           "sources/__init__.py", "tools/resolve_ats.py", "tools/private_tool.py",
           "tests/test_a.py", "tests/fixtures/scoring_prompt_v1.txt", "tests/fixtures/x.json",
           "profile.yaml", "profile.example.yaml"]


# --- Matching -----------------------------------------------------------------


@pytest.mark.parametrize("pattern,path,hit", [
    ("*.py", "main.py", True),
    ("*.py", "sources/ats.py", False),          # * never crosses a "/"
    ("sources/", "sources/ats.py", True),
    ("sources/", "sourcesx/ats.py", False),
    ("tools/resolve_ats.py", "tools/resolve_ats.py", True),
    ("tests/fixtures/*.json", "tests/fixtures/x.json", True),
])
def test_matches(pattern, path, hit):
    assert ep.matches(pattern, path) is hit


def test_select_applies_lines_in_order_and_reports_unmatched():
    files, unmatched = ep.select(["*.py", "!connections.py", "!backfill_linkedin.py", "sources/",
                                  "tests/", "!tests/fixtures/scoring_prompt_v1.txt", "tools/missing.py"],
                                 TRACKED)
    assert files == ["main.py", "sources/__init__.py", "sources/ats.py", "tests/fixtures/x.json",
                     "tests/test_a.py"]
    assert unmatched == ["tools/missing.py"]


def test_never_guard_catches_state_and_profile_but_not_env_example():
    bad = ep.never_violations(["profile.yaml", "seen_jobs.json", "skip_state.json", "state/pending/a.json",
                               ".env", ".env.example", "main.py", "sourcing/parked_boards.csv",
                               "data/dream_companies.txt", "connections.py"])
    assert bad == ["profile.yaml", "seen_jobs.json", "skip_state.json", "state/pending/a.json", ".env",
                   "sourcing/parked_boards.csv", "data/dream_companies.txt", "connections.py"]


def test_the_allowlist_file_selects_nothing_that_must_never_ship():
    patterns = ep.read_allowlist(ep.ALLOWLIST.read_text())
    tracked = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
               if p.is_file() and not {".git", ".venv", "__pycache__"} & set(p.parts)]
    files, _ = ep.select(patterns, tracked)
    assert ep.never_violations(files) == []
    names = {f.rsplit("/", 1)[-1] for f in files}
    assert not names & {"append_jd.py", "rescore_dry_run.py", "build_companies_min.py"}
    for must_not in ("profile.yaml", "ROADMAP.md", "CLAUDE.md", "README.md", "connections.py",
                     "backfill_linkedin.py", "data/companies.yaml",
                     "tests/fixtures/scoring_prompt_v1.txt", "tests/fixtures/weekly_summary_prompt_v1.txt"):
        assert must_not not in files
    for must in ("main.py", "profile.example.yaml", ".env.example", "tools/export/export_public.py",
                 ".github/workflows/daily.yml", "sourcing/harvest.py", "data/us_places.tsv.gz"):
        assert must in files
    assert not [f for f in files if f.startswith("sourcing/") and ".csv" in f]


# --- Tests the export cannot run ------------------------------------------------


def test_a_test_importing_an_unshipped_module_is_found():
    source = {"main.py", "tools/private_tool.py", "tools/resolve_ats.py", "sourcing/harvest.py"}
    export = {"main.py", "tools/resolve_ats.py", "sourcing/harvest.py"}
    code = ("import os\nimport yaml\nimport main\nfrom tools import private_tool\n"
            "from tools.resolve_ats import x\nimport harvest\n")
    assert ep.missing_imports(code, source, export) == ["tools.private_tool"]


def test_import_module_and_py_names_count_as_imports():
    source = {"backfill_linkedin.py", "tools/fix_rows.py", "sourcing/old.py", "main.py", "tools/kept.py"}
    export = {"main.py", "tools/kept.py"}
    code = ("import importlib\nb = importlib.import_module('backfill_linkedin')\n"
            "P = ROOT / 'tools' / 'fix_rows.py'\nM = ROOT / 'main.py'\n"
            "W = ['sourcing/old.py', 'tools/kept.py']\nDATA = ['fix_rows.py', 'old.py']\n")
    assert ep.missing_imports(code, source, export) == ["backfill_linkedin", "fix_rows.py", "sourcing/old.py"]


# --- Generated files --------------------------------------------------------------


SOURCE_ROADMAP = """# Some roadmap

> a note about this repo

## How IDs work

| `PUB` | public engine (some-repo only) |

## Status values

**"Live" is a claim about `origin/main`, not about a branch.** Verify with
`git merge-base --is-ancestor <sha> origin/main` before marking anything Live.
A branch commit changes nothing about what reaches the inbox. This has cost us
once.

## Chat naming

names

## Index

| ID | Feature | Status | Chat name |
|---|---|---|---|
| `SRC-01` | real row | Live | `(done) SRC-01 real` |
"""


def test_roadmap_keeps_the_framework_and_drops_rows_and_history():
    out = ep.roadmap_framework(SOURCE_ROADMAP, "some-repo")
    assert "## How IDs work" in out and "## Chat naming" in out
    assert "git merge-base --is-ancestor" in out
    assert "real row" not in out and "cost us" not in out and "a note about this repo" not in out
    assert "some-repo" not in out
    assert out.rstrip().endswith(ep.ROADMAP_EXAMPLE_ROW)


def test_roadmap_without_its_sections_fails_loudly():
    with pytest.raises(SystemExit):
        ep.roadmap_framework("# nothing here\n", "x")


def test_the_real_roadmap_reduces_cleanly():
    out = ep.roadmap_framework((ROOT / "ROADMAP.md").read_text(), "repo-name-not-present")
    index = out.split("## Index")[1]
    assert [l for l in index.splitlines() if l.startswith("| `")] == [ep.ROADMAP_EXAMPLE_ROW]


# --- Scrub and rewrites ------------------------------------------------------------


def test_scrub_is_whole_word_any_case_and_honours_exemptions(tmp_path):
    (tmp_path / "LICENSE").write_text("Copyright Pat Example\n")
    (tmp_path / "a.py").write_text("x = 1\n# pat example wrote this\n")
    (tmp_path / "b.txt").write_text("compatible patexample\n")
    (tmp_path / "profile.example.yaml").write_text("linkedin_label: my-label\n")
    (tmp_path / "c.md").write_text("label my-label\n")
    items = [("candidate name", "Pat Example", frozenset({"LICENSE"})),
             ("LinkedIn label", "my-label", frozenset({"profile.example.yaml"}))]
    assert ep.scrub(tmp_path, items) == ["a.py:2: candidate name 'Pat Example'",
                                         "c.md:1: LinkedIn label 'my-label'"]


def test_rewrites_apply_and_a_stale_one_stops_the_export(tmp_path, monkeypatch):
    (tmp_path / "t.py").write_text("greeting = 'Hi Pat,'\n")
    monkeypatch.setattr(ep, "REWRITES", [("t.py", "Hi {short},", "Hi Alex,")])
    assert ep.apply_rewrites(tmp_path, {"short": "Pat", "owner": "o"}) == ["rewrote 1 occurrence(s) in t.py"]
    assert (tmp_path / "t.py").read_text() == "greeting = 'Hi Alex,'\n"
    with pytest.raises(SystemExit):
        ep.apply_rewrites(tmp_path, {"short": "Pat", "owner": "o"})


def test_rewrite_for_a_file_that_does_not_ship_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "REWRITES", [("gone.py", "x", "y")])
    assert ep.apply_rewrites(tmp_path, {"short": "Pat", "owner": "o"}) == []


def test_refuses_a_non_empty_out_dir(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        ep.main(["--out", str(tmp_path)])
