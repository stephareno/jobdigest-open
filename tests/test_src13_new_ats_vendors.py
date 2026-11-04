"""SRC-13: ATS vendors added in Sep 2026 -- ADP Workforce Now, UKG Ready, Gem,
Comeet, Manatal, JobScore, and Teamtailor career sites on a company's own domain.

Every vendor needs two edits: detection in tools/resolve_ats.py and a parser in
sources/ats.py. These tests cover both halves per vendor, against trimmed live
payloads saved in tests/fixtures/ats/ on 2026-09-16, plus the two resolver
guards added in the same pass (aggregator pages, opaque-token boards).
No network access.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")

import resolve_ats as ra  # noqa: E402
from sources import ats  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ats"
IT_TITLE = "Director of IT"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def first_posting_titled(ats_name, data, title=IT_TITLE):
    """A copy of the fixture whose first posting carries an IT title."""
    data = copy.deepcopy(data)
    key = {"adp": ("jobRequisitions", "requisitionTitle"),
           "ukg_ready": ("job_requisitions", "job_title"),
           "gem": (None, "title"), "comeet": (None, "name"),
           "manatal": ("results", "position_name"),
           "jobscore": ("jobs", "title"),
           "teamtailor": ("items", "title")}[ats_name]
    postings = data if key[0] is None else data[key[0]]
    postings[0][key[1]] = title
    return data


# ---------------------------------------------------------------- parsers

def test_adp_parser_gates_titles_and_fetches_description_for_matches(monkeypatch):
    detail = fixture("adp_detail")
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return detail

    def fake_get(url, **kw):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(ats.requests, "get", fake_get)
    monkeypatch.setattr(ats, "BOARD_DELAY", 0)
    cid = "cec7cc7f-e8c3-456c-8624-01e71a13a8a7"
    jobs = list(ats.parse_board("Eliquent", "adp", cid, first_posting_titled("adp", fixture("adp"))))

    assert [j["title"] for j in jobs] == [IT_TITLE]
    job = jobs[0]
    assert job["location"] == "Marietta, PA"
    assert job["apply_link"].startswith(
        f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId=598250")
    assert job["description"]          # came from the detail endpoint
    assert job["date_posted"] == "2026-09-14T14:50:00Z"  # full timestamp, UTC (DEL-05)
    assert len(calls) == 1              # detail fetched only for the gated title
    assert ats.posting_count("adp", fixture("adp")) == 3


def test_ukg_ready_parser():
    data = first_posting_titled("ukg_ready", fixture("ukg_ready"))
    jobs = list(ats.parse_board("RFF", "ukg_ready", "secure7.saashr.com:6213629", data))
    assert len(jobs) == 1
    job = jobs[0]
    assert job["location"] == "Washington, DC, USA"
    assert job["apply_link"].startswith("https://secure7.saashr.com/ta/6213629.careers?ShowJob=")
    assert "mission" in job["description"]
    assert ats.posting_count("ukg_ready", fixture("ukg_ready")) == 3


def test_gem_parser_keeps_real_titles_and_work_model():
    data = fixture("gem")
    # "VP of Product" and "VP, Finance & Accounting" are not IT roles.
    assert list(ats.parse_board("Narmi", "gem", "narmi", data)) == []
    jobs = list(ats.parse_board("Narmi", "gem", "narmi", first_posting_titled("gem", data)))
    assert len(jobs) == 1
    assert jobs[0]["apply_link"].startswith("https://jobs.gem.com/narmi/")
    assert jobs[0]["location"] == "New York, United States"
    assert jobs[0]["work_model"] == "hybrid"
    assert ats.posting_count("gem", data) == 3


def test_comeet_parser():
    data = first_posting_titled("comeet", fixture("comeet"))
    jobs = list(ats.parse_board("Zenity", "comeet", "19.000:TOKEN", data))
    # "CTO Office Team Member" also passes: CTO is an IT executive title on its own.
    assert [j["title"] for j in jobs] == [IT_TITLE, "CTO Office Team Member"]
    job = jobs[0]
    assert job["apply_link"].startswith("https://zenity.io/careers/")
    assert "Tel Aviv" in job["location"]  # international: main.py's location filter disqualifies it
    assert job["work_model"] == "in-office"
    assert job["description"]


def test_manatal_parser():
    data = first_posting_titled("manatal", fixture("manatal"))
    jobs = list(ats.parse_board("ExeQut", "manatal", "exequt", data))
    assert len(jobs) == 1
    assert jobs[0]["apply_link"].startswith("https://www.careers-page.com/exequt/job/")
    assert jobs[0]["location"] == "Eastern Province, Saudi Arabia"
    assert ats.posting_count("manatal", fixture("manatal")) == 3


def test_jobscore_parser():
    data = first_posting_titled("jobscore", fixture("jobscore"))
    jobs = list(ats.parse_board("truCurrent", "jobscore", "distributedsun", data))
    assert len(jobs) == 1
    assert jobs[0]["apply_link"].startswith("https://careers.jobscore.com/careers/distributedsun/jobs/")
    assert jobs[0]["work_model"] == "hybrid"
    assert jobs[0]["location"] == "Hybrid (Washington, D.C.)"


def test_teamtailor_career_site_reuses_teamtailor_parser():
    data = first_posting_titled("teamtailor", fixture("teamtailor_site"))
    jobs = list(ats.parse_board("Canoe", "teamtailor", "careers.canoeintelligence.com", data))
    assert len(jobs) == 1
    assert jobs[0]["apply_link"].startswith("https://careers.canoeintelligence.com/jobs/")


# ------------------------------------------------------------- detection

ADP_LINK_PAGE = ('<a href="https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html'
                 '?cid=9b3cf408-35d5-47a9-b102-f592f8abbe88&amp;ccId=19000101_000001&amp;lang=en_US">Jobs</a>')
ADP_WIDGET_PAGE = ("<script src='https://workforcenow.adp.com/mascsr/default/mdf/recwebcomponents/"
                   "recruitment/main-config/recruitment.js'></script><recruitment-current-openings "
                   "cid='cec7cc7f-e8c3-456c-8624-01e71a13a8a7' ccid='19000101_000003' host='DP'>")


@pytest.mark.parametrize("html, page_url, expected", [
    (ADP_LINK_PAGE, "", ("adp", "9b3cf408-35d5-47a9-b102-f592f8abbe88")),
    (ADP_WIDGET_PAGE, "", ("adp", "cec7cc7f-e8c3-456c-8624-01e71a13a8a7")),
    ('<iframe src="https://secure7.saashr.com/ta/6213629.careers?CareersSearch=&amp;lang=en-US">',
     "", ("ukg_ready", "secure7.saashr.com:6213629")),
    ('{"comeet_token":"7682C70163833D81DA042A833D876842A8ED0","comeet_uid":"67.008","comeet_color":""}',
     "", ("comeet", "67.008:7682C70163833D81DA042A833D876842A8ED0")),
    ('<div class="comeet" data-company-uid="e6.00c" data-token="ABCDEF0123456789ABCDEF01">',  # gitleaks:allow (public ATS embed token)
     "", ("comeet", "E6.00C:ABCDEF0123456789ABCDEF01")),
    ('var COMPANY_DATA = {"company_uid": "19.000", "token": "9103660519012202D5024401B3091024403660", "slug": "zenity"}',
     "", ("comeet", "19.000:9103660519012202D5024401B3091024403660")),
    ('<a href="https://jobs.gem.com/narmi">Open roles</a>', "", ("gem", "narmi")),
    ('<a href="https://careers.jobscore.com/careers/accucrete">Jobs</a>', "", ("jobscore", "accucrete")),
    ('<a href="https://www.careers-page.com/exequt">Jobs</a>', "", ("manatal", "exequt")),
    ('<a href="https://axioshq.careers-page.com/">Jobs</a>', "", ("manatal", "axioshq")),
    ('<link href="https://assets-aws.teamtailor-cdn.com/assets/careersite/Inter.woff2">',
     "https://careers.canoeintelligence.com/jobs", ("teamtailor", "careers.canoeintelligence.com")),
])
def test_new_vendor_detection(html, page_url, expected):
    found = [(a, t) for a, t, _ in ra.detect_ats_in_page(html, page_url)]
    assert expected in found


def test_teamtailor_widget_is_not_a_career_site():
    # A jobs.js widget on someone's marketing page: the page host is not the board.
    html = '<script src="https://scripts.teamtailor-cdn.com/widgets/na-maroon/jobs.js"></script>'
    assert ra.detect_ats_in_page(html, "https://about.crunchbase.com/careers") == []


def test_comeet_hosted_link_without_credentials_is_followed():
    html = '<a href="https://www.comeet.com/jobs/zenity/19.000/ai-red-team-researcher/C5.07D">x</a>'
    assert ra.comeet_hosted_pages(html) == ["https://www.comeet.com/jobs/zenity/19.000"]
    with_creds = html + ' data-company-uid="19.000" data-token="9103660519012202D5024401B30910"'
    assert ra.comeet_hosted_pages(with_creds) == []


@pytest.mark.parametrize("ats_name, token, url", [
    ("adp", "9b3cf408-35d5-47a9-b102-f592f8abbe88",
     "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
     "job-requisitions?cid=9b3cf408-35d5-47a9-b102-f592f8abbe88&timeStamp=0&lang=en_US&locale=en_US&$top=100"),
    ("ukg_ready", "secure7.saashr.com:6213629",
     "https://secure7.saashr.com/ta/rest/ui/recruitment/companies/%7C6213629/job-requisitions"
     "?offset=0&size=100&sort=desc&lang=en-US"),
    ("comeet", "19.000:ABC123",
     "https://www.comeet.co/careers-api/2.0/company/19.000/positions?token=ABC123&details=true"),
    ("gem", "narmi", "https://api.gem.com/job_board/v0/narmi/job_posts/"),
    ("manatal", "exequt", "https://core.api.manatal.com/open/v3/career-page/exequt/jobs/?page_size=100"),
    ("jobscore", "accucrete", "https://careers.jobscore.com/jobs/accucrete/feed.json"),
    ("teamtailor", "careers.canoeintelligence.com", "https://careers.canoeintelligence.com/jobs.json"),
    ("teamtailor", "acme", "https://acme.teamtailor.com/jobs.json"),
])
def test_board_api_urls(ats_name, token, url):
    assert ra.board_api_url(ats_name, token) == url


@pytest.mark.parametrize("ats_name, name", [
    ("adp", "adp"), ("ukg_ready", "ukg_ready"), ("gem", "gem"), ("comeet", "comeet"),
    ("manatal", "manatal"), ("jobscore", "jobscore"), ("teamtailor", "teamtailor_site"),
])
def test_validators_accept_live_payloads(ats_name, name):
    assert ra.board_validator(ats_name)(fixture(name))


# ------------------------------------------------------------------ guards

def test_opaque_token_board_needs_to_sit_on_the_companys_own_site():
    data = fixture("adp")
    token = "cec7cc7f-e8c3-456c-8624-01e71a13a8a7"  # gitleaks:allow (public ATS embed token)
    assert ra.board_belongs_to("Brightspot", "adp", token, data,
                               "https://www.brightspot.com/about-us/careers", "brightspot.com")
    # The same widget reached by following a link to another domain proves nothing.
    assert not ra.board_belongs_to("Brightspot", "adp", token, data,
                                   "https://www.someoneelse.com/careers", "brightspot.com")
    # Aggregator pages hand board_belongs_to no payload; opaque boards then never pass.
    assert not ra.board_belongs_to("Brightspot", "adp", token, None,
                                   "https://www.brightspot.com/about-us/careers", "brightspot.com")


def test_aggregator_page_detection():
    one_company = [("greenhouse", "acme", ""), ("greenhouse", "acme", "")]
    stale_plus_current = [("greenhouse", "acme", ""), ("lever", "acme", "")]
    portfolio = [("greenhouse", "eve", ""), ("ashby", "trawa", ""), ("lever", "collate", "")]
    assert not ra.is_aggregator_page(one_company)
    assert not ra.is_aggregator_page(stale_plus_current)
    assert ra.is_aggregator_page(portfolio)


def _careers_run(pages, api, name, seed_url, tmp_path):
    """Run resolve_via_careers against canned pages and board APIs."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
        for prefix, body in api.items():
            if url.startswith(prefix):
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    async def inner():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            report = {}
            hit = await ra.resolve_via_careers(
                client, name, ra.HostThrottle(base_delay=0), asyncio.Semaphore(8),
                ra.PageCache(tmp_path), ra.RobotsCache(), seed_url=seed_url, report=report)
            return hit, report

    return asyncio.run(inner())


def test_vc_portfolio_page_does_not_resolve_to_a_portfolio_board(tmp_path):
    landing = ("<title>Menlo Ventures</title>"
               '<a href="https://boards.greenhouse.io/eve">Eve</a>'
               '<a href="https://jobs.ashbyhq.com/trawa">Trawa</a>'
               '<a href="https://jobs.lever.co/collate">Collate</a>')
    backed = {"jobs": [{"title": "Engineer", "content": "Eve is backed by Menlo Ventures"}]}
    hit, report = _careers_run(
        {"https://menlovc.com/": landing},
        {"https://boards-api.greenhouse.io/v1/boards/eve/": backed,
         "https://api.ashbyhq.com/posting-api/job-board/trawa": {"jobs": [{"title": "x", "descriptionPlain": "Menlo Ventures"}]},
         "https://api.lever.co/v0/postings/collate": [{"text": "x", "description": "Menlo Ventures"}]},
        "Menlo Ventures", "https://menlovc.com/", tmp_path)
    assert hit is None
    assert report["reason"] == "content_check_failed"


def test_adp_widget_on_own_careers_page_resolves(tmp_path):
    landing = "<title>Brightspot</title>" + ADP_WIDGET_PAGE
    cid = "cec7cc7f-e8c3-456c-8624-01e71a13a8a7"
    hit, _ = _careers_run(
        {"https://www.brightspot.com/": landing},
        {"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
         f"job-requisitions?cid={cid}": fixture("adp")},
        "Brightspot", "https://www.brightspot.com/", tmp_path)
    assert hit is not None
    assert (hit["ats"], hit["slug"]) == ("adp", cid)


def test_comeet_resolves_through_its_hosted_page(tmp_path):
    landing = ('<title>Zenity</title><a href="https://www.comeet.com/jobs/zenity/19.000/'
               'ai-red-team-researcher/C5.07D">Open role</a>')
    hosted = 'var COMPANY_DATA = {"company_uid": "19.000", "token": "9103660519012202D5024401B3091024403660"}'
    hit, _ = _careers_run(
        {"https://zenity.io/": landing, "https://www.comeet.com/jobs/zenity/19.000": hosted},
        {"https://www.comeet.co/careers-api/2.0/company/19.000/positions": fixture("comeet")},
        "Zenity", "https://zenity.io/", tmp_path)
    assert hit is not None
    assert (hit["ats"], hit["slug"]) == ("comeet", "19.000:9103660519012202D5024401B3091024403660")
