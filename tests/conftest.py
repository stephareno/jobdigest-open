"""Shared test setup.

DEL-14: the digest's company-profile step fetches company websites and calls
Claude. Tests that run main.digest_run must never do either, so the step is
a no-op everywhere except in its own tests, which inject fakes.

PUB-13: every test runs on the synthetic profile in tests/fixtures, set here
before any test module imports profile_loader (which reads the path once), so
no test depends on the maintainer's profile.yaml or on JOB_DIGEST_PROFILE as
the shell left it."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_PROFILE = ROOT / "tests" / "fixtures" / "profile_test.yaml"
os.environ["JOB_DIGEST_PROFILE"] = str(TEST_PROFILE)

# REV-03: the maintainer's shell exports real credentials (~/.zshrc). A test
# that forgot a stub would then read or write the live Sheet, Doc or inbox, or
# send mail. Replace them with dummies before any module reads them; a test
# that wants a value sets its own with monkeypatch.
for _name in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID", "GMAIL_APP_PASSWORD",
              "ANTHROPIC_API_KEY", "ADZUNA_APP_ID", "ADZUNA_API_KEY", "RAPIDAPI_KEY", "USAJOBS_API_KEY"):
    if os.environ.get(_name):
        os.environ[_name] = "test"

import pytest


@pytest.fixture(scope="session")
def _generated_workflows(tmp_path_factory):
    """Copies of the two scheduled workflows with the test profile's schedule
    written in: the committed files carry the committed profile's."""
    import shutil

    import config
    import profile_loader
    import schedule
    folder = tmp_path_factory.mktemp("workflows")
    files = {name: Path(shutil.copy(path, folder)) for name, path in schedule.WORKFLOWS.items()}
    schedule.write(profile_loader.active()["delivery"], config.CRON_LEAD_MINUTES,
                   config.CRON_OFF_THE_HOUR_MINUTES, workflows=files)
    return files


@pytest.fixture(autouse=True)
def _test_profile_schedule(_generated_workflows, monkeypatch):
    import schedule
    monkeypatch.setattr(schedule, "WORKFLOWS", _generated_workflows)


@pytest.fixture(autouse=True)
def _no_live_company_profiles(request, monkeypatch):
    if request.module.__name__.endswith(("test_del14_company_profiles",
                                         "test_del15_company_site_resolver",
                                         "test_pub14_llm")):
        return
    import profiles
    monkeypatch.setattr(profiles, "enrich_blurbs", lambda jobs, **kw: {})


@pytest.fixture(autouse=True)
def _status_email_cache_in_tmp(tmp_path, monkeypatch):
    """TRK-12: no test reads or writes the committed status_email_cache.json."""
    from sources import status_email
    monkeypatch.setattr(status_email, "CACHE_PATH", tmp_path / "status_email_cache.json")


# REV-03 / REV-04: every way the code reaches the live Sheet, Gmail or the
# inbox is refused, and each refusal is counted. pytest prints the counts at
# the end of every run ("live-service guard: ..."). A refusal is a stub a test
# forgot, never a live call: nothing below the guard is reachable.
LIVE_GUARD_REFUSALS = {}


def _refuser(name):
    def refuse(*a, **k):
        LIVE_GUARD_REFUSALS[name] = LIVE_GUARD_REFUSALS.get(name, 0) + 1
        raise RuntimeError(f"tests never reach the live Google Sheet or mail ({name}; tests/conftest.py)")
    return refuse


@pytest.fixture(autouse=True)
def _no_live_google_sheet(monkeypatch):
    """No test reaches a real Google Sheet, Gmail SMTP or IMAP, whatever
    credentials the shell exports. A test that needs one patches it itself
    (its monkeypatch wins over this one); everything else gets the same
    error an outage gives. Entry points: sheets._get_spreadsheet /
    _get_sheet, gspread.authorize (doctor, sheet-init), smtplib.SMTP_SSL /
    SMTP (emailer, weekly summary, doctor), imaplib.IMAP4_SSL (inbox readers)."""
    import imaplib
    import smtplib
    for module, attr in ((smtplib, "SMTP_SSL"), (smtplib, "SMTP"), (imaplib, "IMAP4_SSL")):
        monkeypatch.setattr(module, attr, _refuser(f"{module.__name__}.{attr}"))
    try:
        import gspread
        monkeypatch.setattr(gspread, "authorize", _refuser("gspread.authorize"))
    except Exception:                                    # noqa: BLE001 -- some tests stub gspread
        pass
    try:
        import sheets
    except Exception:                                    # noqa: BLE001 -- a test that stubs gspread imports it later
        return
    monkeypatch.setattr(sheets, "_get_spreadsheet", _refuser("sheets._get_spreadsheet"))
    monkeypatch.setattr(sheets, "_get_sheet", _refuser("sheets._get_sheet"))


def pytest_terminal_summary(terminalreporter):
    counts = ", ".join(f"{k} {v}" for k, v in sorted(LIVE_GUARD_REFUSALS.items())) or "none"
    terminalreporter.write_line(f"live-service guard: every live Sheet/mail entry point stubbed; "
                                f"refused calls (forgotten stubs): {counts}")


@pytest.fixture(autouse=True)
def _fetch_budget_in_tmp(tmp_path, monkeypatch):
    """REV-06: no test reads or writes the repository's fetch_budget.json."""
    import fetch_budget
    monkeypatch.setattr(fetch_budget, "BUDGET_FILE", str(tmp_path / "fetch_budget.json"))
    fetch_budget.reset_run()
    try:                                                 # REV-07: nor out/calibrate_usage.json
        import calibrate
        monkeypatch.setattr(calibrate, "USAGE_FILE", str(tmp_path / "calibrate_usage.json"))
    except Exception:                                    # noqa: BLE001 -- some tests stub its imports
        pass


@pytest.fixture(autouse=True)
def _sheet_retry_in_tmp(tmp_path, monkeypatch):
    """REV-03: no test reads or writes the repository's state/sheet_retry/."""
    import sheet_retry
    folder = tmp_path / "sheet_retry"
    monkeypatch.setattr(sheet_retry, "retry_dir", lambda: folder)


@pytest.fixture(autouse=True)
def _no_live_model_calls(monkeypatch):
    """PUB-14: every model call goes through llm.py; no test reaches the real
    API. A test that needs replies takes llm_stub."""
    import llm
    from tests.llm_stub import Unstubbed
    monkeypatch.setattr(llm, "_provider", Unstubbed())


@pytest.fixture
def llm_stub(monkeypatch):
    """llm_stub(respond, **kw) installs a StubProvider and returns it."""
    import llm
    from tests.llm_stub import StubProvider

    def install(respond=None, **kw):
        stub = StubProvider(respond, **kw)
        monkeypatch.setattr(llm, "_provider", stub)
        return stub
    return install
