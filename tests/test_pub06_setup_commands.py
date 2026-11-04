"""PUB-06: setup commands — slot job, doctor, setup, profile.example.yaml,
calibrate, digest --dry-run, --help.

No network access and no Claude calls: doctor's network checks, the scorer
and SMTP are stubbed. Nothing in the repo is written: every command that
writes is pointed at tmp_path.
"""

import builtins
import hashlib
import shutil
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for var, value in (("ANTHROPIC_API_KEY", "test"), ("GMAIL_ADDRESS", "test@example.com"),
                   ("GMAIL_APP_PASSWORD", "test")):
    os.environ.setdefault(var, value)

import calibrate  # noqa: E402
import cli  # noqa: E402
import doctor  # noqa: E402
import dry_run  # noqa: E402
import profile_loader  # noqa: E402
import schedule  # noqa: E402
import setup_interview  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
STATE = ["seen_jobs.json", "skip_state.json", "bespoke_state.json", "shard_state.json",
         "status_email_cache.json", "company_profiles.json"]


def _fingerprint():
    """Hash of every state file and queued batch, to prove nothing changed."""
    paths = [ROOT / f for f in STATE] + sorted((ROOT / "state" / "pending").glob("*.json"))
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


# --- Step 1: the slot job --------------------------------------------------------


@pytest.mark.parametrize("name,job", [("daily.yml", "run-digest"), ("weekly_summary.yml", "weekly-summary")])
def test_the_guard_job_has_no_concurrency_and_gates_the_real_job(name, job):
    wf = yaml.safe_load((WORKFLOWS / name).read_text())
    assert "concurrency" not in wf                       # moved to job level
    slot, real = wf["jobs"]["slot"], wf["jobs"][job]
    assert "concurrency" not in slot
    assert slot["outputs"]["run"] == "${{ steps.guard.outputs.run }}"
    assert real["needs"] == "slot" and real["if"] == "needs.slot.outputs.run == 'true'"
    assert real["concurrency"] == {"group": "repo-state-writers", "cancel-in-progress": False}
    checkout = slot["steps"][0]["with"]
    assert checkout["sparse-checkout-cone-mode"] is False
    assert {"main.py", "cli.py", "schedule.py", "profile_loader.py", "profile.yaml"} <= \
        set(checkout["sparse-checkout"].split())
    assert "--scheduled" not in (WORKFLOWS / name).read_text()


def test_cron_write_still_matches_after_the_split():
    import config
    assert schedule.mismatches(profile_loader.active()["delivery"], config.CRON_LEAD_MINUTES,
                               config.CRON_OFF_THE_HOUR_MINUTES) == {}


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        at = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
        return at.astimezone(tz) if tz else at


@pytest.mark.parametrize("cron,run", [("17 7 * * 0-5", "true"), ("17 8 * * 0-5", "false")])
def test_slot_writes_run_to_github_output(monkeypatch, tmp_path, capsys, cron, run):
    monkeypatch.setattr(schedule, "datetime", _Frozen)
    monkeypatch.setenv("SCHEDULED_CRON", cron)
    out = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert cli.early(["slot", "digest"]) == 0
    assert out.read_text() == f"run={run}\n"


def test_slot_needs_only_the_sparse_files(tmp_path):
    """The guard runs from the sparse checkout alone (plus PyYAML)."""
    wf = yaml.safe_load((WORKFLOWS / "daily.yml").read_text())
    for entry in wf["jobs"]["slot"]["steps"][0]["with"]["sparse-checkout"].split():
        # The slot job's checkout holds a real profile; a fresh clone has only the example's.
        src = ROOT / ("profile.example.yaml" if entry == "profile.yaml" else entry)
        dst = tmp_path / entry
        if src.is_dir():
            subprocess.run(["cp", "-R", str(src), str(dst.parent)], check=True)
        else:
            dst.write_bytes(src.read_bytes())
    env = {"PATH": os.environ["PATH"], "SCHEDULED_CRON": "17 6 * * 0-5", "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "main.py", "slot", "digest"], cwd=tmp_path, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] in ("run=true", "run=false")


# --- Step 2: doctor --------------------------------------------------------------


def _clear_secrets(monkeypatch):
    for var in doctor.SECRET_VARS:
        monkeypatch.delenv(var, raising=False)


def test_doctor_phase2_with_no_env_names_each_variable(monkeypatch):
    _clear_secrets(monkeypatch)
    results = {r["check"]: r for r in doctor.phase2()}
    assert set(results) == {"GMAIL imap", "GMAIL smtp", "GOOGLE sheet", "GOOGLE doc", "ANTHROPIC",
                            "ADZUNA", "RAPIDAPI", "USAJOBS"}
    assert all(r["status"] == doctor.FAIL for r in results.values())
    assert results["GMAIL imap"]["reason"].startswith("GMAIL_ADDRESS, GMAIL_APP_PASSWORD missing: set them")
    assert "ANTHROPIC_API_KEY missing" in results["ANTHROPIC"]["reason"]


def test_a_disabled_source_is_skip_not_fail(monkeypatch):
    _clear_secrets(monkeypatch)
    profile = dict(profile_loader.active())
    profile["sources"] = dict(profile["sources"], enabled=dict(profile["sources"]["enabled"], adzuna=False,
                                                                jsearch=False, usajobs=False))
    for check in (doctor.check_adzuna, doctor.check_rapidapi, doctor.check_usajobs):
        r = check(profile)
        assert r["status"] == doctor.SKIP and "is false: set it true" in r["reason"]  # PUB-10: names the switch


def test_doctor_never_prints_a_secret_value(monkeypatch, capsys):
    secret = "sk-ant-SECRET-VALUE-1234"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    def boom(call):
        raise RuntimeError(f"401 invalid x-api-key {secret}")

    import llm
    from tests.llm_stub import StubProvider
    monkeypatch.setattr(llm, "_provider", StubProvider(boom))
    r = doctor.check_anthropic(None)
    assert r["status"] == doctor.FAIL
    assert secret not in r["reason"] and "<ANTHROPIC_API_KEY>" in r["reason"]


def _offline(monkeypatch):
    """Every phase-2 check and the git check answer without the network."""
    monkeypatch.setattr(doctor, "PHASE2", tuple(
        (lambda name: (lambda p: doctor.result(2, name, doctor.PASS, "stubbed")))(f.__name__)
        for f in doctor.PHASE2))
    monkeypatch.setattr(doctor, "_git_remote", lambda: (doctor.SKIP, "stubbed offline"))


def test_doctor_is_read_only_and_ends_with_the_summary(monkeypatch, capsys):
    _offline(monkeypatch)
    real_open = builtins.open

    def read_only(file, mode="r", *a, **k):
        assert not any(c in mode for c in "wax+"), f"doctor opened {file} for writing"
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", read_only)
    before = _fingerprint()
    code = doctor.main([])
    out = capsys.readouterr().out.strip().splitlines()
    assert _fingerprint() == before
    assert out[-1].startswith("DOCTOR all: ") and out[-1].endswith(" warn")
    assert any("[phase 3] PASS  profile" in line for line in out)
    assert any("workflow schedule" in line and "PASS" in line for line in out)
    assert code == (1 if any(" FAIL " in line for line in out) else 0)


def test_doctor_phase_json(monkeypatch, capsys):
    _offline(monkeypatch)
    doctor.main(["--phase", "3", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {r["phase"] for r in data[:-1]} == {3}
    assert data[-1]["summary"].startswith("DOCTOR phase 3: ")


def test_doctor_exits_1_on_a_fail(monkeypatch, capsys):
    _clear_secrets(monkeypatch)
    assert doctor.main(["--phase", "2"]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "DOCTOR phase 2: 0 pass, 8 fail, 0 skip, 0 warn"


def test_required_python_reads_requirements():
    assert doctor.required_python() == (3, 11)


# --- Step 4: profile.example.yaml ------------------------------------------------


def _key_paths(schema, data, prefix=""):
    """Every key path the schema describes as a section, down to its fields
    (a map-valued field such as hard_block is one leaf)."""
    out = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        out.add(path)
        if isinstance(schema.get(key), dict) and isinstance(value, dict):
            out |= _key_paths(schema[key], value, path + ".")
    return out


def test_the_example_validates_and_has_the_same_keys():
    example = yaml.safe_load((ROOT / "profile.example.yaml").read_text())
    committed = profile_loader.read(str(ROOT / "profile.yaml")) if (ROOT / "profile.yaml").exists() \
        else profile_loader.read()      # the test profile, in an export
    assert profile_loader.validate(example) == []
    assert _key_paths(profile_loader.SCHEMA, example) == _key_paths(profile_loader.SCHEMA, committed), \
        "profile.example.yaml and profile.yaml have drifted: add or remove the key in both"


def test_the_example_is_generic():
    text = (ROOT / "profile.example.yaml").read_text()
    for personal in ("gmail.com", "Washington, DC"):
        assert personal not in text
    example = yaml.safe_load(text)
    assert example["candidate"]["pronouns"] == "they/them"
    assert example["delivery"] == dict(example["delivery"], email_to="you@example.com", timezone="America/Chicago")
    assert example["location"]["home"] == "Chicago, IL" and example["location"]["state_tier_overrides"] == {}
    assert example["sources"]["enabled"]["wellfound"] is False
    assert all(v for k, v in example["sources"]["enabled"].items() if k != "wellfound")
    # PUB-16: fictional companies only; the exemplars are dream companies too.
    dreams = [d for d in (ROOT / "data" / "dream_companies.example.txt").read_text().split("\n") if d]
    assert dreams == ["Wayne Enterprises", "Stark Industries", "Pym Technologies"]
    assert example["company_fit"]["exemplars"] == dreams[:2]


# --- Step 3: setup ---------------------------------------------------------------


def _answers(*given):
    it = iter(given)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        try:
            return next(it)
        except StopIteration:
            return ""                                   # Enter: keep the default
    return ask, asked


def test_setup_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "profile.yaml").write_text("keep me")
    said = []
    assert setup_interview.main([], ask=None, say=said.append, profile_path=tmp_path / "profile.yaml",
                                root=tmp_path, run_after=False) == 1
    assert (tmp_path / "profile.yaml").read_text() == "keep me"
    assert "--force" in said[0]


def test_setup_writes_a_valid_profile_and_retries_a_bad_metro(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    (tmp_path / "data").mkdir()
    # PUB-09: Enter for target_department and the two role-criteria lists after
    # the roles answers, and for the three work-model outcomes after relocate.
    # PUB-10: one more Enter for the dealbreakers list.
    ask, asked = _answers("Alex Rivera", "", "they/them", "", "", "", "", "", "", "", "", "", "",
                          "Denver, CO", "Nowhere, ZZ, Boulder, CO", "Boulder, CO", "", "", "", "", "150000")
    said = []
    assert setup_interview.main([], ask=ask, say=said.append, profile_path=tmp_path / "profile.yaml",
                                root=tmp_path, run_after=False) == 0
    data = yaml.safe_load((tmp_path / "profile.yaml").read_text())
    assert profile_loader.validate(data) == []
    assert data["candidate"]["display_name"] == "Alex Rivera" and data["candidate"]["short_name"] == "Alex"
    assert data["location"]["home"] == "Denver, CO"
    assert data["location"]["commutable"] == ["Denver, CO", "Boulder, CO"]
    assert data["compensation"]["floor"] == 150000
    assert data["delivery"]["timezone"] == "America/Denver"      # the guess from TZ
    # Derived from the new location, not the example's Chicago lists.
    assert "locations" not in data["sources"]["adzuna"] and "search_urls" not in data["sources"]["builtin"]
    assert any("Nowhere, ZZ" in s for s in said)                 # the miss was reported, then retried
    assert all(p.split(".")[0].isdigit() for p in asked)         # numbered prompts
    assert (tmp_path / "profile.yaml").read_text().startswith("# profile.yaml — written by")


def test_setup_writes_dream_companies_when_given(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    def ask(prompt):
        return "Acme Robotics, Initech" if ". Dream companies" in prompt else ""
    setup_interview.main([], ask=ask, say=lambda *a: None, profile_path=tmp_path / "profile.yaml",
                         root=tmp_path, run_after=False)
    assert (tmp_path / "data" / "dream_companies.txt").read_text() == "Acme Robotics\nInitech\n"


def test_timezone_guess(monkeypatch):
    monkeypatch.setenv("TZ", ":Europe/London")
    assert setup_interview.guess_timezone() == "Europe/London"
    monkeypatch.setenv("TZ", "Not/AZone")
    monkeypatch.setattr(setup_interview, "_localtime_link", lambda: "")
    assert setup_interview.guess_timezone() is None


# --- Step 5: calibrate -------------------------------------------------------------

# PUB-16: base scores for the twelve samples, chosen so each case's modifiers
# decide its place (tests/test_pub16_example_persona.py replays them under the
# example profile, the one the samples are written for).
STUB = {
    "Wonka Industries": (9, "leads_department"),
    "Pied Piper": (8, "leads_department"),
    "Wayne Enterprises": (7, "leads_department"),
    "Stark Industries": (6, "outside_department"),
    "Vandelay Industries": (5, "leads_department"),
    "Pym Technologies": (8, "leads_department"),
    "Los Pollos Hermanos": (7, "leads_department"),
    "Dunder Mifflin": (5, "leads_department"),
    "Hooli Talent Partners": (7, "leads_department"),
}
EXPECTED_ORDER = [
    "01-strong-fit-hybrid-home.md",       # +1 home, +1 comp (capped at 10)
    "02-strong-fit-remote.md",            # remote, pay between floor and stretch: no modifiers
    "03-dream-company-neutral-sector.md", # +1 home; ties 02 and loses on base score
    "06-relocate-metro-dream.md",         # -2 in a relocate metro, +1 comp
    "10-staffing-firm-client.md",         # scored on the named research client; ties 06 on 7
    "05-heavy-travel.md",                 # the travel criterion's -2 is in the base; +1 home
    "04-wrong-function.md",               # finance systems, not FP&A: function cap
    "08-low-comp.md",                     # -2: stated top below the comp floor
    "07-elsewhere-onsite.md",             # -5 for onsite elsewhere in the US
    "09-hard-block-casino.md",            # hard block by name, no scoring call
    "11-international.md",                # dropped by the location filter, no scoring call
    "12-international-off-planet.md",     # the same, through its London office
]


# The samples' pay is written for the example profile ($140K floor, $190K
# stretch); these lines give the same outcomes under the test profile's
# $100K floor and $150K stretch, in a copy.
SAMPLE_PAY = {"01-strong-fit-hybrid-home.md": ("$175,000 - $195,000", "$135,000 - $155,000"),
              "02-strong-fit-remote.md": ("$160,000 - $185,000", "$120,000 - $145,000"),
              "05-heavy-travel.md": ("$150,000 - $170,000", "$110,000 - $130,000"),
              "07-elsewhere-onsite.md": ("$140,000 - $155,000", "$100,000 - $115,000"),
              "08-low-comp.md": ("$95,000 - $110,000", "$60,000 - $75,000"),
              "10-staffing-firm-client.md": ("$155,000 - $180,000", "$115,000 - $140,000")}


@pytest.fixture
def samples(tmp_path, monkeypatch):
    folder = tmp_path / "samples"
    shutil.copytree(calibrate.DEFAULT_DIR, folder)
    for name, (old, new) in SAMPLE_PAY.items():
        text = (folder / name).read_text()
        assert old in text
        low, high = old.split(" - "), new.split(" - ")
        text = text.replace(old, new).replace(f"{low[0]} to {low[1]}", f"{high[0]} to {high[1]}")
        (folder / name).write_text(text)
    monkeypatch.setattr(calibrate, "DEFAULT_DIR", folder)
    return folder


@pytest.fixture
def stub_scorer(monkeypatch, samples):
    import scorer
    calls = []

    def fake(job, feedback_block=""):
        calls.append(job["company"])
        base, fit = STUB[job["company"]]
        return {"score": base, "rationale": f"{job['company']} stub. Role stub.", "hard_block_industry": None,
                "is_cool_company_wrong_role": False, "work_model": job["work_model"], "loc_city": "",
                "loc_state": "", "hiring_company": "", "website": "", "function_fit": fit}

    # A real model call fails the test: conftest installs llm_stub.Unstubbed.
    monkeypatch.setattr(scorer, "_score_single", fake)
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    return calls


def test_calibrate_ranks_the_samples_in_the_implied_order(stub_scorer, samples):
    before = _fingerprint()
    ranked, calls = calibrate.run(str(samples))
    assert [j["file"] for j in ranked] == EXPECTED_ORDER
    assert calls == 9                       # the casino (name pre-filter) and both non-US make no call
    assert _fingerprint() == before
    finals = {j["file"]: j["score"] for j in ranked}
    assert finals["01-strong-fit-hybrid-home.md"] == 10 and finals["06-relocate-metro-dream.md"] == 7
    assert finals["04-wrong-function.md"] == 4
    assert finals["08-low-comp.md"] == 3 and finals["07-elsewhere-onsite.md"] == 2


def test_calibrate_table_and_drivers(stub_scorer, samples, capsys):
    assert calibrate.main(["--dir", str(samples)]) == 0
    out = capsys.readouterr().out
    assert "+1 home, +1 comp, cap 10" in out
    assert "location.relocate: would-relocate (Seattle, WA, 0 mi) (-2)" in out
    assert "compensation.floor: $100,000 vs a stated top of $75,000 (-2)" in out
    assert "industries.hard_block: gambling/casinos (name pre-filter)" in out
    assert "location.outside_country: disqualify (outside US)" in out   # PUB-09: was location.international
    assert "scoring.function_cap: 4 (function_fit outside_department)" in out
    # PUB-12: the cost lines follow the calls line.
    assert "Claude calls made: 0. Postings scored: 9. Nothing was written (no Sheet, no seen_jobs.json, no queue).\nCost: " in out


def test_calibrate_needs_the_key(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert calibrate.main(["--dir", str(calibrate.DEFAULT_DIR)]) == 1
    assert "ANTHROPIC_API_KEY missing" in capsys.readouterr().out


def test_every_sample_is_fictional_and_sized():
    for path in sorted((ROOT / "examples" / "postings").glob("*.md")):
        job = calibrate.parse_posting(path)
        assert job["title"] and job["company"] and job["location"] and job["work_model"], path.name
        assert 200 <= len(path.read_text().split()) <= 400, path.name


# --- Step 6: digest --dry-run ---------------------------------------------------------


@pytest.fixture
def out_file(monkeypatch, tmp_path):
    path = tmp_path / "out" / "digest_dry_run.html"
    monkeypatch.setattr(dry_run, "OUT", str(path))
    return path


class _SMTP:
    sent = []

    def __init__(self, host, port):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        pass

    def sendmail(self, sender, to, message):
        _SMTP.sent.append((to, message))


def test_dry_run_from_the_queue_changes_no_state(monkeypatch, out_file, capsys):
    import emailer
    _SMTP.sent = []
    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", _SMTP)
    before = _fingerprint()
    assert dry_run.main(["--send"]) == 0
    assert _fingerprint() == before
    assert out_file.exists() and out_file.read_text().lstrip().startswith("<")
    (to, message), = _SMTP.sent
    assert to == profile_loader.active()["delivery"]["email_to"]
    import email
    assert str(email.header.make_header(email.header.decode_header(
        email.message_from_string(message)["Subject"]))).startswith("[DRY RUN] Job digest")
    assert "queue, Sheet and state files untouched" in capsys.readouterr().out


def test_dry_run_from_calibrate(stub_scorer, out_file, capsys):
    before = _fingerprint()
    assert dry_run.main(["--from-calibrate"]) == 0
    assert _fingerprint() == before
    html = out_file.read_text()
    assert "Wonka Industries" in html and "Gringotts" not in html
    # 01, 02, 03, 06 and the staffing client's 7 reach the main section; Vandelay's 6 is worth a look.
    assert "[DryRun] top matches 5, worth a look 1" in capsys.readouterr().out


def test_dry_run_send_needs_gmail(monkeypatch, out_file, capsys):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "")
    assert dry_run.main(["--send"]) == 1
    assert not out_file.exists()


# --- Step 7: help ------------------------------------------------------------------


def test_help_lists_every_command():
    out = subprocess.run([sys.executable, "main.py", "--help"], cwd=ROOT, capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"]})
    assert out.returncode == 0, out.stderr
    for command in ("fetch", "digest", "all", "profile-check", "cron", "doctor", "setup", "calibrate"):
        assert any(line.strip().startswith(command + " ") for line in out.stdout.splitlines()), command


def test_doctor_and_setup_run_with_no_secrets(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "JOB_DIGEST_PROFILE": os.environ["JOB_DIGEST_PROFILE"]}
    out = subprocess.run([sys.executable, "main.py", "doctor", "--phase", "3"], cwd=ROOT, capture_output=True,
                         text=True, env=env)
    assert out.stdout.strip().splitlines()[-1].startswith("DOCTOR phase 3: "), out.stderr
    existing = tmp_path / "profile.yaml"
    existing.write_text("version: 1\n")
    said = []
    assert setup_interview.main([], say=said.append, profile_path=str(existing), run_after=False) == 1
    assert any("already exists; nothing written" in line for line in said)
    assert existing.read_text() == "version: 1\n"
