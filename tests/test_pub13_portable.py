"""PUB-13: the suite runs on tests/fixtures/profile_test.yaml, so it passes with
or without the maintainer's files; connections.py is optional; the export check's
defaults; corrections.csv; `resolve --retry-unchecked`; the stricter check for a
guessed domain. No network access.
"""

import asyncio
import csv
import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for var, value in (("ANTHROPIC_API_KEY", "test"), ("GMAIL_ADDRESS", "test@example.com"),
                   ("GMAIL_APP_PASSWORD", "test"), ("GOOGLE_SHEET_ID", "x"),
                   ("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")):
    os.environ.setdefault(var, value)

import company_lists  # noqa: E402
import main  # noqa: E402
import profile_loader  # noqa: E402
import prompt_template  # noqa: E402
import resolve  # noqa: E402
import websites  # noqa: E402
from tools import build_public_boards  # noqa: E402

spec = importlib.util.spec_from_file_location("export_public", ROOT / "tools" / "export" / "export_public.py")
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

REPO_PROFILES = [p for p in ("profile.yaml", "profile.example.yaml") if (ROOT / p).exists()]


# --- The profiles -----------------------------------------------------------------------


def test_the_suite_runs_on_the_test_profile():
    assert profile_loader.active()["candidate"]["display_name"] == "Sam Testcase"
    assert Path(profile_loader.DEFAULT_PATH).name == "profile_test.yaml"


@pytest.mark.parametrize("name", REPO_PROFILES)
def test_a_repo_profile_validates_and_renders(name):
    data = profile_loader.read(str(ROOT / name))
    assert profile_loader.validate(data) == []
    prompt = prompt_template.render((ROOT / "prompts" / "scoring_system.txt").read_text(),
                                    prompt_template.context(profile_loader.load(str(ROOT / name))))
    assert data["candidate"]["display_name"] in prompt


@pytest.mark.parametrize("name", REPO_PROFILES)
def test_the_test_profile_copies_no_candidate_value(name):
    mine = profile_loader.read(str(ROOT / name))["candidate"]
    test = profile_loader.active()["candidate"]
    assert not {v for k, v in test.items() if k != "pronouns"} & set(mine.values())


# --- Optional connections ---------------------------------------------------------------


def test_with_no_connections_module_the_match_is_off_and_says_so_once(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "connections", None)
    assert main.load_connections() == {}
    assert main.find_warm_contact("Acme", {}) == ""
    assert capsys.readouterr().out == "[Connections] connections.py not found: warm contact match off\n"


def test_the_export_writes_no_connections_stub():
    assert "connections" not in ep.write_generated.__code__.co_names
    assert not hasattr(ep, "CONNECTIONS_STUB")


# --- Export check -----------------------------------------------------------------------


def test_a_skip_naming_a_profile_is_caught():
    output = ("SKIPPED [2] tests/test_a.py:3: needs the committed PROFILE\n"
              "SKIPPED [1] tests/test_b.py:9: no network here\n"
              "1 passed, 3 skipped\n")
    assert ep.profile_skips(output) == ["SKIPPED [2] tests/test_a.py:3: needs the committed PROFILE"]


def test_a_path_alone_is_not_a_reason():
    assert ep.profile_skips("SKIPPED [1] tests/test_profile.py:9: no network here\n") == []


def test_unset_ids_are_named():
    assert ep.unscanned({"GOOGLE_DOC_ID": "x"}) == ["GOOGLE_SHEET_ID"]
    assert ep.unscanned({"GOOGLE_DOC_ID": "x", "GOOGLE_SHEET_ID": "y"}) == []


# --- Corrections ------------------------------------------------------------------------


def _write(path, text):
    path.write_text(text)
    return str(path)


@pytest.fixture
def corrections(tmp_path, monkeypatch):
    path = _write(tmp_path / "corrections.csv", "name,field,value,reason\n"
                                                "Wrong Co,outcome,none,matched another company\n"
                                                "Moved Co,board,lever https://api.lever.co/v0/postings/moved,moved\n"
                                                "Site Co,website,https://site.example,typo\n")
    monkeypatch.setattr(company_lists, "CORRECTIONS", path)
    return path


def test_corrections_win_over_a_list_row(corrections):
    rows = [{"name": "wrong co", "outcome": "board", "ats": "lever", "url": "u", "website": "w"},
            {"name": "Moved Co", "outcome": "none", "ats": "", "url": "", "website": ""},
            {"name": "Site Co", "website": "https://old.example", "website_source": "guess", "outcome": ""},
            {"name": "Left Alone", "outcome": "board"}]
    company_lists.apply_corrections(rows)
    assert (rows[0]["outcome"], rows[0]["ats"], rows[0]["url"]) == ("none", "", "")
    assert (rows[1]["outcome"], rows[1]["ats"]) == ("board", "lever")
    assert rows[1]["url"] == "https://api.lever.co/v0/postings/moved"
    assert (rows[2]["website"], rows[2]["website_source"]) == ("https://site.example", "correction")
    assert rows[3] == {"name": "Left Alone", "outcome": "board"}


def test_corrections_rewrite_a_boards_file(corrections):
    got = company_lists.correct_boards([{"name": "Wrong Co", "ats": "lever", "url": "u", "website": ""},
                                        {"name": "Moved Co", "ats": "ashby", "url": "old", "website": ""},
                                        {"name": "Kept Co", "ats": "lever", "url": "k", "website": ""}])
    assert [(b["name"], b["ats"]) for b in got] == [("Kept Co", "lever"), ("Moved Co", "lever")]


def test_a_correction_with_an_unknown_field_stops(tmp_path):
    path = _write(tmp_path / "c.csv", "name,field,value,reason\nAcme,board_url,x,y\n")
    with pytest.raises(ValueError, match="field must be one of"):
        company_lists.load_corrections(path)


def test_resolve_reads_a_list_through_the_corrections(corrections, tmp_path):
    listed = _write(tmp_path / "list.csv", "name,website,outcome,notes\nWrong Co,,,\nOther Co,,,\n")
    companies = company_lists.load([{"file": listed, "tier": "watch"}])
    assert [resolve.needs_board(c) for c in companies] == [False, True]


def test_a_rebuild_keeps_the_correction(corrections, tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("# built\nname,website,website_source,outcome,notes\nWrong Co,,,,\n")
    rows = build_public_boards.update_list({"wrong co": "board"}, {}, path=path)
    assert rows[0]["outcome"] == "none"
    assert path.read_text().startswith("# built\n")


# --- resolve --retry-unchecked ----------------------------------------------------------


@pytest.fixture
def retry_list(tmp_path):
    path = tmp_path / "public_companies_us.csv"
    path.write_text("# built\nname,website,website_source,outcome,notes\n"
                    "Found Co,,,none/workable_unchecked,\n"
                    "Empty Co,,,none/workable_unchecked,\n"
                    "Blocked Co,,,none/workable_unchecked,\n"
                    "Settled Co,,,none,\n"
                    "Board Co,,,board,\n")
    (tmp_path / "starter_boards.csv").write_text("name,ats,url,website\n")
    return path


def _outcomes(path):
    return {r["name"]: r["outcome"] for r in company_lists.read_list(str(path))}


def _fake_probe(asked, blocked=()):
    async def probe(name):
        asked.append(name)
        if name in blocked:
            raise resolve.WorkableBlocked()
        return {"ats": "workable", "board_url": "https://apply.workable.com/api/v1/accounts/found"} \
            if name == "Found Co" else None
    return probe


def test_retry_probes_only_the_unchecked_and_writes_the_answers(retry_list):
    asked, log = [], []
    assert resolve.run_retry(str(retry_list), yes=True, probe=_fake_probe(asked), log=log.append) == 0
    assert asked == ["Found Co", "Empty Co", "Blocked Co"]
    assert _outcomes(retry_list) == {"Found Co": "board", "Empty Co": "none", "Blocked Co": "none",
                                     "Settled Co": "none", "Board Co": "board"}
    board = company_lists.read_list(str(retry_list.parent / "starter_boards.csv"))
    assert [(b["name"], b["ats"]) for b in board] == [("Found Co", "workable")]
    assert company_lists.board_token("workable", board[0]["url"]) == "found"
    assert "[Retry] probed 3: 1 boards found, 2 nothing found, 0 not reached (still unchecked)" in log


def test_the_give_up_rule_leaves_the_rest_unchecked(retry_list):
    log = []
    resolve.run_retry(str(retry_list), yes=True, probe=_fake_probe([], blocked={"Empty Co"}), log=log.append)
    outcomes = _outcomes(retry_list)
    assert outcomes["Found Co"] == "board"
    assert outcomes["Empty Co"] == outcomes["Blocked Co"] == resolve.UNCHECKED
    assert "[Retry] probed 1: 1 boards found, 0 nothing found, 2 not reached (still unchecked)" in log


def test_dry_run_counts_and_sends_nothing(retry_list):
    before, log, asked = retry_list.read_text(), [], []
    resolve.run_retry(str(retry_list), dry_run=True, probe=_fake_probe(asked), log=log.append)
    assert asked == [] and retry_list.read_text() == before
    assert log[0].startswith("[Retry] 3 companies are none/workable_unchecked; this run probes 3")
    assert log[-1] == "[Retry] --dry-run: nothing probed, nothing written"


def test_max_limits_the_batch(retry_list):
    asked = []
    resolve.run_retry(str(retry_list), max_n=1, yes=True, probe=_fake_probe(asked), log=lambda s: None)
    assert asked == ["Found Co"]


def test_report_only_writes_nothing(retry_list):
    before = retry_list.read_text()
    resolve.run_retry(str(retry_list), report_only=True, yes=True, probe=_fake_probe([]), log=lambda s: None)
    assert retry_list.read_text() == before


def test_dry_run_needs_retry_unchecked(capsys):
    with pytest.raises(SystemExit):
        resolve.main(["--dry-run"])
    assert "--dry-run goes with --retry-unchecked" in capsys.readouterr().err


def test_the_monthly_job_runs_the_retry_and_reports_it():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "resolve_ats.yml").read_text())
    steps = wf["jobs"]["resolve"]["steps"]
    names = [s.get("name") for s in steps]
    assert names.index("Resolve newly listed companies") < names.index("Retry companies Workable has not checked") \
        < next(i for i, n in enumerate(names) if n.startswith("Open PR"))
    retry = steps[names.index("Retry companies Workable has not checked")]
    assert "--retry-unchecked" in retry["run"] and "--max" in retry["run"]
    assert "$RETRY" in steps[-1]["run"] and "data/company_lists/starter_boards.csv" in steps[-1]["run"]


# --- The stricter check for a guessed domain ---------------------------------------------


@pytest.mark.parametrize("name,token", [("ACME ROBOTICS INC", "acme"), ("The Boeing Co", "boeing"),
                                        ("Velo3D, Inc.", "velo3d"), ("3M CO", ""), ("BJ's Corp", ""),
                                        ("Co-Diagnostics, Inc.", "codiagnostics")])
def test_distinctive_token(name, token):
    assert websites.distinctive_token(name) == token


@pytest.mark.parametrize("html,ok", [
    ("<title>Acme Robotics | Home</title>", True),
    ("<title>Home</title><meta property='og:site_name' content='Acme'>", True),
    ('<title>Home</title><meta content="Acme" name="og:site_name">', True),
    ("<title>Home</title><p>Acme Robotics everywhere in the body</p>", False),
])
def test_title_or_site_name_must_hold_the_token(html, ok):
    assert websites.title_names_company(html, "ACME ROBOTICS INC") is ok


def test_entities_are_read_and_a_name_with_no_token_passes():
    assert websites.title_names_company("<title>Mama&#039;s Creations</title>", "Mama's Creations, Inc.")
    assert websites.title_names_company("<title>Home</title>", "3M CO")


def test_the_domain_alone_carries_the_token():
    html = "<title>Home</title>"
    assert websites.title_names_company(html, "Kimball Electronics, Inc.", "https://kimballelectronics.com")
    assert websites.title_names_company(html, "Crescent Energy Co", "https://crescent-energy.com")
    assert not websites.title_names_company(html, "National Energy Services Reunited Corp.", "https://www.nesr.com")


class _Http:
    def __init__(self, title):
        self.html = f"<title>{title}</title><p>Northwind Trading Partners</p>"

    def head(self, url):
        return 200, url, ""

    def get(self, url):
        return 200, url, self.html


def test_a_guess_that_fails_is_rejected_and_one_that_passes_is_kept(monkeypatch):
    # An initials domain is the guess whose host need not hold the name token.
    monkeypatch.setattr(websites, "domain_guesses", lambda name: ["https://ntp.com"])
    log = []
    ladder = websites.Ladder(_Http("NTP Home"), log=log.append)
    assert ladder.find({"name": "Northwind Trading Partners Inc"}) == ("", websites.GUESS_REJECTED)
    assert log == ["[Websites] guessed domain rejected: Northwind Trading Partners Inc -> https://ntp.com "
                   "(guessed domain, name not on page)"]
    site, source = websites.Ladder(_Http("NTP | Northwind"), log=log.append).find({"name": "Northwind Trading Partners Inc"})
    assert (site, source) == ("https://ntp.com", "guess")


def test_a_rejected_guess_is_recorded_as_nothing_found_with_the_note():
    class Ladder:
        def find(self, row):
            return "", websites.GUESS_REJECTED

    async def resolver(name, website):
        raise AssertionError("a rejected guess is resolved no further")

    async def workable(name):
        raise AssertionError("a rejected guess gets no Workable probe")
    resolver.workable = workable
    company = company_lists.Company("Acme Inc", "", "", "watch", "x.csv", {"name": "Acme Inc"})
    ((hit, report, deferred, website, source),) = asyncio.run(resolve._resolve_all([company], resolver, Ladder()))
    assert hit is None and website == "" and resolve.classify(hit, report, deferred) == \
        ("nothing found", websites.GUESS_REJECTED_NOTE)


def test_the_status_message_uses_the_candidates_pronoun():
    from sources import status_email as se
    fields = {"from": "a@b.c", "date": "d", "subject": "s", "body": "b"}
    assert se.build_user_message(fields, []).startswith("Candidate rows from their job index:\n")


def test_the_two_notes_agree():
    assert resolve.GUESS_REJECTED_NOTE == websites.GUESS_REJECTED_NOTE


def test_build_public_boards_reads_the_note_as_none(tmp_path):
    with open(tmp_path / "parked_boards.csv", "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["company", "reason"])
        writer.writeheader()
        writer.writerow({"company": "Acme", "reason": f"resolve: {websites.GUESS_REJECTED_NOTE}"})
    assert build_public_boards.collect(str(tmp_path))[1] == {"acme": "none"}
