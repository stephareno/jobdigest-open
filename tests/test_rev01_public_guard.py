"""REV-01: personal state is never pushed to a public repository.

fetch.yml, daily.yml and weekly_summary.yml carry a guard step before any
step that touches state (and before any git add); the slot jobs do not. The
step's shell is run here against a stub gh. doctor's phase 1 checks the
repository's visibility. delivery.allow_public_repo (default false, a bool)
turns both failures into warnings.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import doctor  # noqa: E402
import profile_loader  # noqa: E402

GUARD = "Refuse to commit state to a public repository"
WORKFLOWS = {"fetch.yml": "fetch", "daily.yml": "run-digest", "weekly_summary.yml": "weekly-summary"}


def _workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _guard(name):
    steps = _workflow(name)["jobs"][WORKFLOWS[name]]["steps"]
    return next(s for s in steps if s.get("name") == GUARD)


# --- The workflow step ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_the_guard_comes_right_after_install_and_before_any_state_step(name):
    steps = _workflow(name)["jobs"][WORKFLOWS[name]]["steps"]
    names = [s.get("name") for s in steps]
    guard = names.index(GUARD)
    # Only checkout, Python and the hash-pinned install come first: none touches state.
    assert names[:guard] == ["Checkout repository", "Set up Python", "Install dependencies"]
    adds = [i for i, s in enumerate(steps) if "git add" in (s.get("run") or "")]
    assert adds and all(i > guard for i in adds)
    assert all("git add" not in (s.get("run") or "") and "main.py" not in (s.get("run") or "")
               for s in steps[:guard])


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_a_commit_step_never_runs_after_the_guard_refused(name):
    for step in _workflow(name)["jobs"][WORKFLOWS[name]]["steps"]:
        if "git add" in (step.get("run") or ""):
            cond = step.get("if", "success()")
            assert "always()" not in cond or "steps.public-guard.outcome == 'success'" in cond, (name, cond)


def test_the_three_steps_are_identical_and_the_slot_jobs_have_none():
    steps = [_guard(n) for n in sorted(WORKFLOWS)]
    assert all(s == steps[0] for s in steps)
    assert steps[0]["id"] == "public-guard"
    assert steps[0]["env"] == {"GH_TOKEN": "${{ github.token }}",
                               "EVENT_PRIVATE": "${{ github.event.repository.private }}",
                               "EVENT_FORK": "${{ github.event.repository.fork }}"}
    for name in ("daily.yml", "weekly_summary.yml"):
        assert all(s.get("name") != GUARD for s in _workflow(name)["jobs"]["slot"]["steps"])


def test_the_step_reads_the_override_with_python_and_says_what_doctor_says():
    run = _guard("fetch.yml")["run"]
    assert "python -c" in run and "allow_public_repo" in run
    assert f"::error::{doctor.PUBLIC_REPO_MESSAGE} Nothing was written." in run


def _run_guard(tmp_path, *, private="true", fork="false", api_fails=False,
               event_private="", event_fork="", allow=None):
    """Run the step's shell in tmp_path with a stub gh; (exit code, output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answers = {".private": private, ".fork": fork}
    stub = "#!/bin/sh\n" + ("exit 1\n" if api_fails else
                            "".join(f'[ "$4" = "{k}" ] && echo {v} && exit 0\n' for k, v in answers.items()) + "exit 1\n")
    (bin_dir / "gh").write_text(stub)
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')   # the venv's, with PyYAML
    (bin_dir / "python").chmod(0o755)
    delivery = {} if allow is None else {"allow_public_repo": allow}
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump({"delivery": delivery}))
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GITHUB_REPOSITORY="someone/agent",
               GH_TOKEN="x", EVENT_PRIVATE=event_private, EVENT_FORK=event_fork)
    out = subprocess.run(["bash", "-e", "-c", _guard("fetch.yml")["run"]], cwd=tmp_path, env=env,
                         capture_output=True, text=True)
    return out.returncode, out.stdout + out.stderr


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize("kw,code,says", [
    ({}, 0, "Repository is private and not a fork."),
    ({"private": "false"}, 1, "::error::This repository is public or a fork."),
    ({"fork": "true"}, 1, "::error::This repository is public or a fork."),
    ({"private": "false", "allow": True}, 0, "::warning::This repository is public or a fork, and delivery.allow_public_repo is true"),
    ({"private": "false", "allow": "true"}, 1, "::error::"),          # only a real YAML true counts
    ({"api_fails": True, "event_private": "true", "event_fork": "false"}, 0, "private and not a fork"),
    ({"api_fails": True, "event_private": "false"}, 1, "::error::"),
    ({"api_fails": True}, 1, "::warning::Could not read this repository's visibility"),   # fails closed
    ({"event_private": "false"}, 1, "::error::"),                     # the event overrides a private API answer
])
def test_the_step_itself(tmp_path, kw, code, says):
    rc, out = _run_guard(tmp_path, **kw)
    assert rc == code, out
    assert says in out, out


# --- doctor ------------------------------------------------------------------------------


def _gh(stdout="", returncode=0, stderr=""):
    def run(cmd, **kw):
        assert cmd == ["gh", "repo", "view", "--json", "isPrivate,isFork"]
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


def _answer(private, fork):
    return _gh(json.dumps({"isPrivate": private, "isFork": fork}))


def test_doctor_private_and_not_a_fork_passes():
    assert doctor._repo_visibility(_answer(True, False), allow=False) == (doctor.PASS, "private, not a fork.")


def test_doctor_public_fails():
    assert doctor._repo_visibility(_answer(False, False), allow=False) == (doctor.FAIL, doctor.PUBLIC_REPO_MESSAGE)


def test_doctor_fork_fails_even_when_private():
    assert doctor._repo_visibility(_answer(True, True), allow=False) == (doctor.FAIL, doctor.PUBLIC_REPO_MESSAGE)


def test_doctor_public_with_the_override_warns():
    status, reason = doctor._repo_visibility(_answer(False, False), allow=True)
    assert status == doctor.WARN
    assert "public" in reason and "allow_public_repo is true" in reason


def test_doctor_warns_with_the_reason_when_gh_is_missing_or_fails():
    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    status, reason = doctor._repo_visibility(missing, allow=False)
    assert status == doctor.WARN and "not installed" in reason
    status, reason = doctor._repo_visibility(_gh(returncode=1, stderr="not logged in"), allow=False)
    assert status == doctor.WARN and "not logged in" in reason
    status, reason = doctor._repo_visibility(_gh("not json"), allow=False)
    assert status == doctor.WARN


def test_doctor_phase1_reports_the_check(monkeypatch):
    monkeypatch.setattr(doctor, "_repo_visibility", lambda: (doctor.FAIL, doctor.PUBLIC_REPO_MESSAGE))
    monkeypatch.setattr(doctor, "_git_remote", lambda: (doctor.PASS, "ok"))
    rows = [r for r in doctor.phase1() if r["check"] == "repository visibility"]
    assert rows == [doctor.result(1, "repository visibility", doctor.FAIL, doctor.PUBLIC_REPO_MESSAGE)]


def test_doctor_reads_the_override_from_the_profile(monkeypatch):
    monkeypatch.setattr(doctor, "_profile_or_none", lambda: {"delivery": {"allow_public_repo": True}})
    assert doctor._allow_public_repo() is True
    monkeypatch.setattr(doctor, "_profile_or_none", lambda: None)
    assert doctor._allow_public_repo() is False


# --- The profile override -------------------------------------------------------------


def test_the_override_defaults_false_and_both_profiles_say_false():
    assert profile_loader.DEFAULTS["delivery.allow_public_repo"] is False
    # profile.yaml is not in an export; the test fixture stands in for it there.
    for name in ("profile.yaml", "profile.example.yaml", "tests/fixtures/profile_test.yaml"):
        if not (ROOT / name).exists():
            continue
        data = profile_loader.read(ROOT / name)
        assert data["delivery"]["allow_public_repo"] is False
        assert profile_loader.load(ROOT / name)["delivery"]["allow_public_repo"] is False


def test_the_override_may_be_left_out_and_then_is_false():
    data = profile_loader.read(ROOT / "profile.example.yaml")
    del data["delivery"]["allow_public_repo"]
    assert profile_loader.validate(data) == []
    assert profile_loader.with_defaults(data)["delivery"]["allow_public_repo"] is False


@pytest.mark.parametrize("value", ["true", "yes", 1, None, [True]])
def test_the_override_must_be_a_bool(value):
    data = profile_loader.read(ROOT / "profile.example.yaml")
    data["delivery"]["allow_public_repo"] = value
    errors = profile_loader.validate(data)
    assert any("delivery.allow_public_repo" in e for e in errors), errors
