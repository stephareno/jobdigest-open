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
