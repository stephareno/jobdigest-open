"""PUB-10: setup plumbing — .env autoload and .env.example, sheet-init, live
calibrate, opt-in inbox readers, doctor fixes, generic function-fit names,
dealbreaker criteria, the sample digest and setup --from-document."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import envfile
import sheet_init
from sheet_layout import DISQUALIFIED_COLUMNS, OPTIONAL_COLUMNS, SHEET_COLUMNS, SHEET_NAME, \
    DISQUALIFIED_SHEET_NAME

ROOT = Path(__file__).resolve().parent.parent


# --- STEP 1: .env ------------------------------------------------------------------

def test_env_example_matches_the_secrets_table():
    assert (ROOT / ".env.example").read_text() == envfile.example_text(), \
        "run python envfile.py to regenerate .env.example"


def test_env_example_lists_every_secret_once_with_no_value():
    lines = (ROOT / ".env.example").read_text().splitlines()
    assigned = [l for l in lines if l and not l.startswith("#")]
    assert assigned == [f"{name}=" for name in envfile.NAMES]


def test_env_example_is_not_gitignored():
    done = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT)
    assert done.returncode == 1
    assert subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0


def test_load_reads_pairs_comments_quotes_and_export(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a comment\n\nA=1\nexport B = two words # trailing\nC=\"quoted # kept\"\n"
                    "D='{\"json\": true}'\nE=\n")
    env = {}
    assert envfile.load(str(path), env) == ["A", "B", "C", "D"]
    assert env == {"A": "1", "B": "two words", "C": "quoted # kept", "D": '{"json": true}'}


def test_load_never_overrides_the_environment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB=from-file\n")
    env = {"A": "from-shell"}
    assert envfile.load(str(path), env) == ["B"]
    assert env["A"] == "from-shell"


def test_load_without_a_file_sets_nothing(tmp_path):
    env = {}
    assert envfile.load(str(tmp_path / ".env"), env) == [] and env == {}


def test_a_bad_line_names_its_number_never_its_text(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nsk-secret-value-with-no-equals\n")
    with pytest.raises(SystemExit) as e:
        envfile.load(str(path), {})
    assert "line 2" in str(e.value) and "sk-secret" not in str(e.value)


def test_main_loads_dotenv_before_anything_reads_the_environment():
    src = (ROOT / "main.py").read_text()
    assert src.index("envfile.load()") < src.index("import cli")


def test_doctor_scrubs_every_secret_in_the_table():
    import doctor
    assert set(doctor.SECRET_VARS) == set(envfile.NAMES)


# --- STEP 2: sheet-init -------------------------------------------------------------

class FakeWorksheet:
    def __init__(self, title, values=None):
        self.title = title
        self.values = [list(r) for r in (values or [])]
        self.frozen = None
        self.calls = []

    def get_all_values(self):
        self.calls.append("get_all_values")
        return [list(r) for r in self.values]

    def update(self, values, range_name):
        assert range_name == "A1"
        self.calls.append("update")
        row = list(values[0])
        if self.values:
            self.values[0] = row + self.values[0][len(row):]
        else:
            self.values = [row]

    def batch_clear(self, ranges):
        self.calls.append("batch_clear")
        width = int(ranges[0].split(":R1C")[0].split("C")[-1]) - 1
        self.values[0] = self.values[0][:width]

    def format(self, rng, fmt):
        self.calls.append("format")

    def freeze(self, rows=None):
        self.calls.append("freeze")
        self.frozen = rows


class FakeBook:
    def __init__(self, tabs):
        self.tabs = tabs

    def worksheets(self):
        return list(self.tabs)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(title)
        self.tabs.append(ws)
        return ws


SA = '{"client_email": "digest@proj.iam.gserviceaccount.com", "private_key": "x"}'


@pytest.fixture
def sheet_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", SA)
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet123")


def _run(book, capsys):
    code = sheet_init.main([], open_book=lambda info, sid: book)
    return code, capsys.readouterr().out


def test_sheet_init_creates_both_tabs_with_headers_and_freezes(sheet_env, capsys):
    book = FakeBook([FakeWorksheet("Sheet1")])
    code, out = _run(book, capsys)
    assert code == 0
    tabs = {ws.title: ws for ws in book.tabs}
    assert tabs[SHEET_NAME].values == [SHEET_COLUMNS + OPTIONAL_COLUMNS]
    assert tabs[DISQUALIFIED_SHEET_NAME].values == [DISQUALIFIED_COLUMNS]
    assert tabs[SHEET_NAME].frozen == 1 and tabs[DISQUALIFIED_SHEET_NAME].frozen == 1
    assert "Acting as digest@proj.iam.gserviceaccount.com" in out
    assert "'Sheet1'" in out and tabs["Sheet1"].calls == []


def test_sheet_init_is_idempotent(sheet_env, capsys):
    book = FakeBook([])
    assert _run(book, capsys)[0] == 0
    before = [ws.values for ws in book.tabs]
    code, out = _run(book, capsys)
    assert code == 0 and [ws.values for ws in book.tabs] == before
    assert out.count("already initialised") == 2
    assert all("update" not in ws.calls[-2:] for ws in book.tabs)


def test_sheet_init_refuses_a_tab_with_data_and_says_which(sheet_env, capsys):
    main = FakeWorksheet(SHEET_NAME, [SHEET_COLUMNS[:3], ["", "", ""], ["CIO", "Acme", "Remote"]])
    book = FakeBook([main])
    code, out = _run(book, capsys)
    assert code == 1
    assert f"REFUSED '{SHEET_NAME}': NOT touched" in out and "first at row 3" in out
    assert main.calls == ["get_all_values"]
    assert {ws.title: ws.values for ws in book.tabs}[DISQUALIFIED_SHEET_NAME] == [DISQUALIFIED_COLUMNS]


def test_sheet_init_completes_a_partial_header_on_an_empty_tab(sheet_env, capsys):
    tab = FakeWorksheet(DISQUALIFIED_SHEET_NAME, [["Job Title", "Company", "x", "y", "z"] + ["w"] * 20])
    book = FakeBook([tab])
    assert _run(book, capsys)[0] == 0
    assert tab.values == [DISQUALIFIED_COLUMNS]


def test_sheet_init_names_missing_variables(monkeypatch, capsys):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    assert sheet_init.main([], open_book=lambda *a: pytest.fail("must not open")) == 1
    out = capsys.readouterr().out
    assert "GOOGLE_SERVICE_ACCOUNT_JSON missing" in out and "GOOGLE_SHEET_ID missing" in out


def test_sheet_init_is_dispatched_before_the_pipeline():
    import cli
    assert "sheet-init" in cli.HELP
    src = (ROOT / "cli.py").read_text()
    assert 'if cmd == "sheet-init":' in src


# --- STEP 3: live calibrate ---------------------------------------------------------

import builtins

import calibrate


def _posting(title, company, location="Remote, US", source=None, n=0):
    job = {"title": title, "company": company, "location": location, "work_model": "Remote", "comp": "",
           "description": "Leads the internal IT team.", "apply_link": f"https://jobs.example/{company}/{n}"}
    if source:
        job["source"] = source
    return job


LIVE = {
    "adzuna": [_posting("Director of IT", "Kestrel Robotics", n=1),
               _posting("Senior Software Engineer", "Kestrel Robotics", n=2),
               _posting("VP, Information Technology", "Fjord Systems", location="Oslo, Norway", n=3)],
    "jsearch": [_posting("Head of IT", "Prairie Freight", n=4),
                _posting("Director of IT", "Kestrel Robotics", n=5)],          # a repeat: listed once
}


@pytest.fixture
def live(monkeypatch):
    """Stub fetchers and keys; records every file opened for writing."""
    for name in ("ADZUNA_APP_ID", "ADZUNA_API_KEY", "RAPIDAPI_KEY"):
        monkeypatch.setenv(name, "test")
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.setattr(calibrate, "_fetcher", lambda name: (lambda: [dict(j) for j in LIVE.get(name, [])]))
    writes = []
    real = builtins.open

    def recording(file, mode="r", *a, **k):
        if set(str(mode)) & set("wax+"):
            writes.append(os.fspath(file))
        return real(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", recording)
    return writes


def _state_writes(writes):
    return [w for w in writes if calibrate._protected(w)]


def test_titles_lists_passed_and_failed_with_counts_and_no_model_call(live, monkeypatch, capsys):
    import scorer
    monkeypatch.setattr(scorer, "_score_single", lambda *a, **k: pytest.fail("no model call in --titles"))
    assert calibrate.main(["--titles"]) == 0
    out = capsys.readouterr().out
    passed = out.split("PASSED (")[1].split("FAILED (")[0]
    failed = out.split("FAILED (")[1].split("Per source:")[0]
    assert passed.startswith("2)") and "Director of IT" in passed and "Head of IT" in passed
    assert "title gate      Senior Software Engineer" in failed
    assert "outside US      VP, Information Technology" in failed
    assert "  adzuna           3       1       1        1" in out
    assert "  jsearch          2       1       0        0" in out
    assert "usajobs     not run: USAJOBS_API_KEY missing" in out
    assert "slow sources (ats, startups, bespoke, watchlist) run only when named" in out
    assert _state_writes(live) == []


def test_live_calibrate_scores_up_to_max_and_writes_nothing(live, stub_live_scorer, capsys):
    assert calibrate.main(["--max", "2"]) == 0
    out = capsys.readouterr().out
    assert len(stub_live_scorer) == 2
    assert "scoring 2 of 2 postings that passed" in out
    assert "What drove the ranking:" in out and "Claude calls made: 0. Postings scored: 2." in out
    assert "scoring.role_criteria: " in out
    assert _state_writes(live) == []


@pytest.fixture
def stub_live_scorer(monkeypatch):
    import profile_loader
    import scorer
    first = profile_loader.active()["scoring"]["role_criteria"][0]["id"]
    calls = []

    def fake(job, feedback_block=""):
        calls.append(job["company"])
        return {"score": 7, "rationale": "Stub company. Stub role.", "hard_block_industry": None,
                "is_cool_company_wrong_role": False, "work_model": "Remote", "loc_city": "", "loc_state": "",
                "hiring_company": "", "website": "", "function_fit": "leads_department",
                "criteria_hits": [first]}

    monkeypatch.setattr(scorer, "_score_single", fake)
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    return calls


def test_live_calibrate_needs_the_anthropic_key_but_titles_does_not(live, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert calibrate.main([]) == 1
    assert "ANTHROPIC_API_KEY missing" in capsys.readouterr().out
    assert calibrate.main(["--titles"]) == 0


def test_a_source_that_tries_to_write_state_is_stopped(live, monkeypatch, tmp_path):
    target = os.path.join(calibrate.ROOT, "seen_jobs.json")
    before = Path(target).read_bytes() if os.path.exists(target) else None

    def sneaky():
        with open(target, "w") as fh:
            fh.write("{}")
        return []

    monkeypatch.setattr(calibrate, "_fetcher", lambda name: sneaky)
    with calibrate.no_state_writes():
        with pytest.raises(PermissionError):
            calibrate.fetch_live(["adzuna"])
        with pytest.raises(PermissionError):
            os.replace(tmp_path / "x", os.path.join(calibrate.ROOT, "state", "pending", "batch.json"))
    assert (Path(target).read_bytes() if os.path.exists(target) else None) == before
    assert builtins.open is not None and open.__name__ != "guarded_open"


def test_slow_sources_run_only_when_named():
    enabled = {"adzuna": True, "jsearch": True, "builtin": False, "usajobs": True, "ats": True}
    assert calibrate.pick_sources(None, enabled)[0] == ["adzuna", "jsearch", "usajobs"]
    picked, notes = calibrate.pick_sources(["ats", "builtin"], enabled)
    assert picked == ["ats", "builtin"]
    assert any("[ats] is a slow source" in n for n in notes)
    assert any("[builtin] disabled in profile.yaml" in n for n in notes)
    with pytest.raises(SystemExit):
        calibrate.pick_sources(["linkedin_email"], enabled)


def test_dir_keeps_the_offline_mode_and_rejects_live_flags(capsys):
    with pytest.raises(SystemExit):
        calibrate.main(["--dir", "docs/samples", "--titles"])


# --- STEP 4: opt-in inbox readers ----------------------------------------------------

import json
import types

import yaml

import doctor
import profile_loader


def _with_sources(**changes):
    profile = profile_loader.with_defaults(profile_loader.active())
    for key, value in changes.items():
        if key in ("status_tracking", "applied_doc"):
            profile["sources"][key]["enabled"] = value
        else:
            profile["sources"]["enabled"][key] = value
    return profile


def test_the_example_turns_both_readers_off():
    example = profile_loader.load(str(ROOT / "profile.example.yaml"))
    assert example["sources"]["status_tracking"]["enabled"] is False
    assert example["sources"]["applied_doc"]["enabled"] is False
    text = (ROOT / "profile.example.yaml").read_text()
    assert "Reads the Gmail inbox of GMAIL_ADDRESS" in text and "Reads the Google Doc in GOOGLE_DOC_ID" in text


def test_readers_default_off_when_the_profile_leaves_them_out():
    raw = profile_loader.read()
    del raw["sources"]["status_tracking"], raw["sources"]["applied_doc"]
    assert profile_loader.validate(raw) == []
    full = profile_loader.with_defaults(raw)
    assert full["sources"]["status_tracking"] == {"enabled": False}
    assert full["sources"]["applied_doc"] == {"enabled": False}


@pytest.mark.parametrize("doc,status", [(True, True), (False, True), (True, False), (False, False)])
def test_the_digest_runs_each_reader_only_when_enabled(monkeypatch, capsys, doc, status):
    import main
    ran = []
    monkeypatch.setitem(sys.modules, "sources.applied_doc",
                        types.SimpleNamespace(seed_from_doc=lambda: ran.append("doc")))
    monkeypatch.setitem(sys.modules, "sources.status_email",
                        types.SimpleNamespace(check_status_emails=lambda: ran.append("status")))
    main.run_inbox_readers({"applied_doc": {"enabled": doc}, "status_tracking": {"enabled": status}})
    assert ran == [n for n, on in (("doc", doc), ("status", status)) if on]
    out = capsys.readouterr().out
    assert ("sources.applied_doc.enabled is false" in out) == (not doc)
    assert ("sources.status_tracking.enabled is false" in out) == (not status)


def _clear_secrets(monkeypatch):
    for name in envfile.NAMES:
        monkeypatch.delenv(name, raising=False)


def test_doctor_skips_the_doc_when_applied_doc_is_off(monkeypatch):
    _clear_secrets(monkeypatch)
    r = doctor.check_google_doc(_with_sources(applied_doc=False))
    assert r["status"] == doctor.SKIP and "sources.applied_doc.enabled is false" in r["reason"]
    assert doctor.check_google_doc(_with_sources(applied_doc=True))["status"] == doctor.FAIL


def test_doctor_skips_imap_only_when_nothing_reads_the_inbox(monkeypatch):
    _clear_secrets(monkeypatch)
    off = doctor.check_gmail_imap(_with_sources(linkedin_email=False, status_tracking=False))
    assert off["status"] == doctor.SKIP
    assert "sources.enabled.linkedin_email" in off["reason"] and "sources.status_tracking.enabled" in off["reason"]
    for on in ({"linkedin_email": True, "status_tracking": False}, {"linkedin_email": False, "status_tracking": True}):
        assert doctor.check_gmail_imap(_with_sources(**on))["status"] == doctor.FAIL


# --- STEP 5: doctor fixes -------------------------------------------------------------


def test_missing_secret_names_where_to_get_it(monkeypatch):
    _clear_secrets(monkeypatch)
    r = doctor.check_anthropic(None)
    assert r["status"] == doctor.FAIL and "console.anthropic.com" in r["reason"] and ".env" in r["reason"]


def test_git_remote_needing_credentials_is_warn_and_exit_0(monkeypatch, capsys):
    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return types.SimpleNamespace(returncode=0, stdout="https://github.com/someone/private.git\n", stderr="")
        return types.SimpleNamespace(returncode=128, stdout="", stderr="fatal: Authentication failed for ...")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    status, reason = doctor._git_remote()
    assert status == doctor.WARN and "gh auth login" in reason
    monkeypatch.setattr(doctor, "phase1", lambda: [doctor.result(1, "git remote", status, reason)])
    assert doctor.main(["--phase", "1"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert "[phase 1] WARN  git remote" in out[0]
    assert out[-1] == "DOCTOR phase 1: 0 pass, 0 fail, 0 skip, 1 warn"
    doctor.main(["--phase", "1", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["status"] == "WARN" and data[-1]["summary"].endswith("1 warn")


def test_no_profile_note_and_optional_sources_fail(monkeypatch, capsys, tmp_path):
    _clear_secrets(monkeypatch)
    monkeypatch.setattr(profile_loader, "DEFAULT_PATH", str(tmp_path / "profile.yaml"))
    monkeypatch.setattr(doctor, "_profile_or_none", lambda: None)
    assert doctor.main(["--phase", "2"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == f"[phase 2] NOTE  {doctor.NO_PROFILE_NOTE}"
    assert doctor.NO_PROFILE_NOTE == "no profile yet; optional sources show FAIL until phase 3 marks them disabled"
    assert any(" FAIL  ADZUNA " in l for l in out) and any(" FAIL  GOOGLE doc " in l for l in out)
    doctor.main(["--phase", "2", "--json"])
    assert json.loads(capsys.readouterr().out)[0] == {"note": doctor.NO_PROFILE_NOTE}
    doctor.main(["--phase", "3"])
    out = capsys.readouterr().out
    assert "NOTE" not in out and "profile.yaml not found: run python main.py setup" in out
    monkeypatch.setattr(doctor, "phase1", lambda: [doctor.result(1, "python", doctor.PASS, "ok")])
    doctor.main([])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("[phase 1] PASS") and lines[1] == f"[phase 2] NOTE  {doctor.NO_PROFILE_NOTE}"


# --- STEP 6: generic names -------------------------------------------------------------

import prompt_template


def _prompt(raw):
    return prompt_template.render((ROOT / "prompts" / "scoring_system.txt").read_text(),
                                  prompt_template.context(raw))


def test_function_fit_note_renders_only_when_present():
    raw = profile_loader.read()
    assert "A \"Supervisory IT Specialist\" who manages a team counts." in _prompt(raw)
    raw["roles"]["function_fit_note"] = ""
    text = _prompt(raw)
    assert "Supervisory" not in text
    assert "and owns a\n  team or a budget.\n- \"in_department\"" in text
    del raw["roles"]["function_fit_note"]
    assert profile_loader.validate(raw) == [] and "Supervisory" not in _prompt(raw)


def test_example_profile_leaves_the_note_empty():
    example = profile_loader.read(str(ROOT / "profile.example.yaml"))
    assert example["roles"]["function_fit_note"] == ""
    assert "Supervisory" not in _prompt(example)


def test_old_target_function_key_says_it_was_renamed():
    raw = profile_loader.read()
    raw["roles"]["target_function"] = raw["roles"].pop("target_department")
    errors = profile_loader.validate(raw)
    assert "roles.target_function: renamed to roles.target_department" in errors
    assert "roles.target_department: required field is missing" in errors


def test_roles_comments_say_what_each_field_is_matched_against():
    for name in ("profile.yaml", "profile.example.yaml"):
        if not (ROOT / name).exists():
            continue                                     # a public clone has only the example
        text = (ROOT / name).read_text()
        roles = text[text.index("\nroles:"):text.index("\nlocation:")]
        assert "matched against the posting's TITLE only" in roles
        assert "whole words or phrases, case-insensitive" in roles
        assert "target_department is judged by the model from the job DESCRIPTION" in roles
        assert "capped at scoring.function_cap" in roles


@pytest.mark.parametrize("old,new", [("it_leadership", "leads_department"), ("IT_Other", "in_department"),
                                     ("not_it", "outside_department"), ("leads_department", "leads_department"),
                                     ("something", "unknown"), (None, None)])
def test_function_fit_value_maps_old_names(old, new):
    import config
    assert config.function_fit_value(old) == new


def test_queued_batch_with_old_names_reads_as_new(tmp_path, monkeypatch):
    import pending
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path)
    (tmp_path / "20261001T000000.json").write_text(json.dumps(
        {"jobs": [{"title": "t", "company": "c", "function_fit": "not_it"}]}))
    _, jobs, _ = pending.load_pending()
    assert jobs[0]["function_fit"] == "outside_department"


# --- STEP 7: dealbreaker criteria ------------------------------------------------------

import llm
import location
import scorer
from tests.llm_stub import StubProvider

NO_TEAM = {"id": "no_team", "dealbreaker": True, "criterion": "Individual contributor role with no team"}


def _criteria_errors(entry):
    raw = profile_loader.read()
    raw["scoring"]["role_criteria"].append(entry)
    return [e for e in profile_loader.validate(raw) if "role_criteria" in e]


@pytest.mark.parametrize("entry,ok", [
    (NO_TEAM, True),
    ({**NO_TEAM, "points": 0}, True),
    ({**NO_TEAM, "points": -3}, False),                      # both
    ({"id": "x", "criterion": "y"}, False),                  # neither
    ({**NO_TEAM, "dealbreaker": "yes"}, False),
    ({"id": "x", "points": 2, "dealbreaker": False, "criterion": "y"}, True),
])
def test_an_entry_has_points_or_dealbreaker_not_both(entry, ok):
    errors = _criteria_errors(entry)
    assert (errors == []) == ok, errors
    if entry.get("points") == -3:
        assert "has points (-3) and dealbreaker: true; give one or the other" in errors[0]


def test_dealbreakers_render_in_their_own_block_and_in_the_ids():
    raw = profile_loader.read()
    raw["scoring"]["role_criteria"].append(NO_TEAM)
    text = _prompt(raw)
    assert ("\nDEALBREAKERS: report these in criteria_hits; do not score them.\n"
            "A dealbreaker applies only when the posting states it plainly. If the posting\n"
            "is silent or ambiguous, it does not apply. Report one only if you can point\n"
            "to the sentence in the posting that shows it. Judge it on the role as posted,\n"
            "in whatever department: a role outside the target department is\n"
            "function_fit's question, never a dealbreaker's.\n"
            "- no_team: Individual contributor role with no team\n") in text     # PUB-11 wording
    ids = text.split('"criteria_hits": ["<the id of each SCORING criterion that applies, from: ')[1].split(">")[0]
    assert ids.endswith(", no_team")
    scoring = text.split("SCORING (")[1].split("Hard disqualify")[0]
    assert "no team" not in scoring.lower()                         # never a scored line


def test_no_dealbreakers_leaves_the_prompt_unchanged():
    assert "DEALBREAKERS" not in _prompt(profile_loader.read())


def test_example_has_one_generic_dealbreaker():
    example = profile_loader.read(str(ROOT / "profile.example.yaml"))
    deals = [c for c in example["scoring"]["role_criteria"] if c.get("dealbreaker")]
    assert deals == [NO_TEAM]
    assert not any(c.get("dealbreaker") for c in profile_loader.read()["scoring"]["role_criteria"])


@pytest.fixture
def with_no_team(monkeypatch):
    raw = profile_loader.read()
    raw["scoring"]["role_criteria"].append(NO_TEAM)
    profile = profile_loader.with_defaults(raw)
    monkeypatch.setattr(location, "CONFIG", location.build_config(profile))
    monkeypatch.setattr(scorer, "PROFILE", profile)
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)


def _reply(hits, score=9):
    payload = {"score": score, "criteria_hits": hits,
               "rationale": {"company": "Robot maker.", "role": "Owns internal IT.", "culture": "candid"},
               "hard_block_industry": None, "is_cool_company_wrong_role": False,
               "function_fit": "leads_department", "work_model": "Remote", "city": None, "state": None,
               "hiring_company": "Acme", "website": "acme.com"}
    return StubProvider(lambda call: json.dumps(payload))


def _scored(monkeypatch, hits, score=9):
    monkeypatch.setattr(llm, "_provider", _reply(hits, score))
    job = {"title": "VP IT", "company": "Acme", "location": "Remote, US", "work_model": "Remote",
           "description": "", "comp": "Pay range: $250,000 - $300,000"}
    scorer.score_jobs([job])
    return job


def test_a_reported_dealbreaker_scores_zero_with_the_reason(with_no_team, monkeypatch):
    first = profile_loader.read()["scoring"]["role_criteria"][0]["id"]
    job = _scored(monkeypatch, [first, "no_team"])
    assert job["score"] == 0 and job["base_score"] == 9
    assert job["dealbreaker"] == "no team" and job["dealbreaker_criterion"] == "no_team"
    blurb, notes = location.split_rationale(job["rationale"])
    assert notes == "Dealbreaker: no team."
    assert scorer.is_disqualified(job)
    sections = scorer.filter_jobs([job])
    assert all(job not in sections[k] for k in ("main", "worth_a_look", "cool_wrong_role"))
    # Re-tiering keeps it and never names it twice.
    location.apply_location_tier(job, base_score=job["base_score"], record=False)
    assert job["score"] == 0 and job["rationale"].count("Dealbreaker:") == 1


def test_no_dealbreaker_hit_leaves_the_score_alone(with_no_team, monkeypatch):
    first = profile_loader.read()["scoring"]["role_criteria"][0]["id"]
    job = _scored(monkeypatch, [first], score=8)
    assert job["score"] > 0 and not job.get("dealbreaker") and not scorer.is_disqualified(job)
    assert "Dealbreaker" not in job["rationale"]


def test_the_code_decides_even_when_the_model_returns_zero(with_no_team, monkeypatch):
    job = _scored(monkeypatch, ["no_team"], score=0)
    assert job["score"] == 0 and job["dealbreaker"] == "no team" and scorer.is_disqualified(job)


# --- STEP 9: setup --from-document -----------------------------------------------------

import zipfile

import setup_document
import setup_interview

SENTINEL = "Zanzibar-Quokka-7731"     # appears only in the document, never in a proposal

PROPOSAL = {
    "kind": "resume",
    "background": ["15 years leading internal IT", "Ran a 40-person infrastructure team"],
    "target_department": "an internal IT or business systems function",
    "target_titles": ["VP of IT", "Head of IT"],
    "role_criteria": [{"points": 3, "criterion": "Owns the IT budget and vendor decisions"},
                      {"points": 1, "criterion": "Reports to the CEO or COO"},
                      {"points": -2, "criterion": "Help-desk management only"},
                      {"points": 0, "criterion": "dropped: zero points"}],
}


def DocClient(reply=PROPOSAL):
    """The stub provider for setup --from-document: answers `reply`."""
    return StubProvider(lambda call: reply if isinstance(reply, str) else json.dumps(reply))


def _setup(tmp_path, argv, client=None):
    (tmp_path / "data").mkdir(exist_ok=True)
    from test_pub06_setup_commands import _answers
    ask, asked = _answers("Alex Rivera", "", "they/them")
    said = []
    previous = llm.use(client or DocClient())
    try:
        code = setup_interview.main(argv, ask=ask, say=said.append, profile_path=tmp_path / "profile.yaml",
                                    root=tmp_path, run_after=False)
    finally:
        llm.use(previous)
    return code, said, asked


def test_from_document_proposes_defaults_and_never_writes_the_document(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TZ", "America/Denver")
    doc = tmp_path / "resume.md"
    doc.write_text(f"# Alex Rivera\nalex@example.com\n{SENTINEL}\nVP of IT at Acme.\n")
    client = DocClient()
    code, said, _ = _setup(tmp_path, ["--from-document", str(doc)], client)
    assert code == 0 and len(client.calls) == 1
    assert SENTINEL in json.dumps(client.calls[0]["messages"])
    written = (tmp_path / "profile.yaml").read_text()
    assert SENTINEL not in written and "alex@example.com" not in written
    data = yaml.safe_load(written)
    assert data["candidate"]["background"] == "- 15 years leading internal IT\n- Ran a 40-person infrastructure team"
    assert data["roles"]["target_department"] == "an internal IT or business systems function"
    assert data["roles"]["target_titles"] == ["VP of IT", "Head of IT"]
    crit = data["scoring"]["role_criteria"]
    assert [(c.get("points"), c["criterion"]) for c in crit if not c.get("dealbreaker")] == [
        (3, "Owns the IT budget and vendor decisions"), (1, "Reports to the CEO or COO"),
        (-2, "Help-desk management only")]
    assert [c["id"] for c in crit if c.get("dealbreaker")] == ["no_team"]     # the example's, kept
    assert profile_loader.validate(data) == []
    assert any("target_titles: VP of IT, Head of IT" in s for s in said)


def test_from_document_without_a_key_runs_the_normal_interview(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TZ", "America/Denver")
    doc = tmp_path / "resume.txt"
    doc.write_text("resume")
    client = DocClient()
    code, said, _ = _setup(tmp_path, ["--from-document", str(doc)], client)
    assert code == 0 and client.calls == []
    assert "ANTHROPIC_API_KEY is not set" in said[0] and "normal interview" in said[0]
    example = profile_loader.read(str(ROOT / "profile.example.yaml"))
    assert yaml.safe_load((tmp_path / "profile.yaml").read_text())["roles"]["target_titles"] == \
        example["roles"]["target_titles"]


def test_an_unreadable_format_is_skipped_with_a_message(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TZ", "America/Denver")
    doc = tmp_path / "resume.rtf"
    doc.write_text("{\\rtf1 resume}")
    client = DocClient()
    code, said, _ = _setup(tmp_path, ["--from-document", str(doc)], client)
    assert code == 0 and client.calls == []
    assert any(".rtf is not a format setup reads" in s for s in said)


def test_docx_text_is_read_with_the_standard_library(tmp_path):
    path = tmp_path / "jd.docx"
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = (f'<w:document {w}><w:body><w:p><w:r><w:t>Director of IT</w:t></w:r></w:p>'
           f'<w:p><w:r><w:t>Leads </w:t></w:r><w:r><w:t>a team of 12</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)
    assert setup_document.read_document(str(path)) == ("text", "Director of IT\nLeads a team of 12")


def test_pdf_goes_to_the_model_as_a_document(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    client = DocClient({"kind": "job_description", "background": ["ignored for a JD"],
                        "target_titles": ["CIO"]})
    monkeypatch.setattr(llm, "_provider", client)
    proposal, problem = setup_document.propose(str(path), "claude-sonnet-4-6")
    assert problem is None and proposal == {"target_titles": ["CIO"]}
    call = client.calls[0]
    assert call["attachments"] == [{"media_type": "application/pdf", "data": b"%PDF-1.4 fake"}]
    # PUB-14: the Anthropic provider sends it as a document block, ahead of the prompt.
    block = llm._with_attachments(call["messages"], call["attachments"])[0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"


def test_a_bad_reply_leaves_the_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("TZ", "America/Denver")
    doc = tmp_path / "resume.txt"
    doc.write_text("resume")
    code, said, _ = _setup(tmp_path, ["--from-document", str(doc)], DocClient("not json"))
    assert code == 0 and any("model call failed" in s and "normal interview" in s for s in said)


# --- STEP 8: the public sample digest ----------------------------------------------------

import re


def _denylist():
    """The maintainer's personal values where profile.yaml exists (the test
    profile's in an export), read at test time so the test file itself carries
    none: candidate name, short name, email, the employers named in the
    background ("... at Acme.ai"), and GOOGLE_DOC_ID when it is set in the
    environment."""
    path = ROOT / "profile.yaml"
    raw = profile_loader.read(str(path) if path.exists() else profile_loader.DEFAULT_PATH)
    c = raw["candidate"]
    values = {c["display_name"], c["short_name"], raw["delivery"]["email_to"]}
    for employer in re.findall(r"\bat ([A-Z][\w&.-]*(?: [A-Z][\w&.-]*)*)", c["background"]):
        values |= {employer, employer.split(".")[0]}
    doc_id = os.environ.get("GOOGLE_DOC_ID", "").strip()
    if doc_id:
        values.add(doc_id)
    return {v for v in values if len(v) >= 3}


# PUB-16: the Sheet snapshot and the saved calibrate results the two renders read.
@pytest.mark.parametrize("name", ["sample_digest.html", "sample_sheet.html", "sample_calibrate.json"])
def test_sample_renders_carry_no_personal_values(name):
    deny = _denylist()
    assert len(deny) >= 3                         # name, short name, email
    text = (ROOT / "docs" / name).read_text().casefold()
    # Whole words: a short name ("Sam") must not match inside "Sample".
    found = sorted(v for v in deny if re.search(r"(?<!\w)" + re.escape(v.casefold()) + r"(?!\w)", text))
    assert found == [], f"docs/{name} must be rendered under profile.example.yaml (tools/render_sample_sheet.py)"
