"""DEL-14: company profiles from each emailed role's own website rewrite the
blurb's company and culture content; profiles never feed the score.

Covers: who gets profiled (an unnamed staffing client is skipped); site
resolution order (harvest, companies.yaml, the apply link only when it is the
company's own); the fetch (homepage plus the first two subpages with real
text, JS-only shells count as nothing, robots.txt honoured, a redirect moves
the paths); a namesake's site is not profiled; the cache (keyed by domain, a
fresh hit makes no request and no profile call, TTL, transient errors not
cached); the rewrite keeps the score notes and the score, an over-cap reply
keeps the old blurb, and any failure leaves the blurb as it is. No network.
"""

import importlib
import json
import os
import sys
import types
from datetime import date
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("GOOGLE_SHEET_ID", "x")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
for name in ("gspread", "google", "google.oauth2", "google.oauth2.service_account",
             "googleapiclient", "googleapiclient.discovery"):
    try:
        importlib.import_module(name)
    except ImportError:
        module = types.ModuleType(name)
        module.Credentials = object
        module.build = lambda *a, **k: None
        module.service_account = module
        sys.modules[name] = module

import llm  # noqa: E402
import profiles  # noqa: E402
from tests import llm_stub  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402

TODAY = date(2026, 10, 15)
LONG = "We build industrial cybersecurity for the grid. " * 20          # ~1,000 chars
VALUES = "Our values: transparency, curiosity, long-term thinking. " * 10
OLD_BLURB = ("~600-person, industrial cybersecurity. Top IT seat (no CIO). Owns corporate IT, "
             "service desk, SaaS, vendors. Culture: no signal in posting.")
NOTES = "Comp tops out at $256K, $240K+: +1."
# The rewrite may only change the company fragment and the culture keywords.
# A "role" in the reply (here a demoted one) must never reach the blurb.
NEW = {"company": "~600-person, OT cybersecurity for utilities, manufacturers.",
       "role": "Senior systems seat.",
       "culture": "transparency, curiosity, long-term thinking."}
NEW_BLURB = ("~600-person, OT cybersecurity for utilities, manufacturers. Top IT seat (no CIO). "
             "Owns corporate IT, service desk, SaaS, vendors. Culture: transparency, curiosity, "
             "long-term thinking.")


def _page(text):
    return f"<html><body><main><p>{text}</p></main></body></html>"


def _transport(routes, seen=None):
    def handler(request):
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        if url in routes:
            status, body, *headers = routes[url]
            return httpx.Response(status, text=body, headers=(headers[0] if headers else
                                                               {"content-type": "text/html"}))
        return httpx.Response(404, text="", headers={"content-type": "text/html"})
    return httpx.MockTransport(handler)


def _factory(routes, seen=None):
    return lambda: httpx.Client(transport=_transport(routes, seen), follow_redirects=True)


class FakeClient(StubProvider):
    """The stub provider (PUB-14): profile replies for PROFILE_SYSTEM, NEW for
    the rewrite. `calls` lists each call's kind; `requests` (kind, content)."""

    def __init__(self, profile=None, rewrite=None, fail=False):
        self.profile = profile or {"company_matches": True, "what": "OT cybersecurity",
                                   "who_for": "utilities", "size": None,
                                   "mission": "protect civilization",
                                   "culture": ["transparency", "curiosity"]}
        self.rewrite = rewrite or NEW
        self.fail = fail
        self.requests = []
        super().__init__(self._reply, usage={"input_tokens": 1000, "output_tokens": 100})

    @staticmethod
    def kind(call):
        return "profile" if call["system"] == profiles.PROFILE_SYSTEM else "rewrite"

    def record(self, call):
        self.calls.append(self.kind(call))
        self.requests.append((self.kind(call), call["content"]))

    def _reply(self, call):
        if self.fail:
            raise RuntimeError("API down")
        return json.dumps(self.profile if self.kind(call) == "profile" else self.rewrite)


def _enrich(jobs, client=None, **kw):
    """profiles.enrich_blurbs with `client` as the model provider."""
    previous = llm.use(client or FakeClient())
    try:
        return profiles.enrich_blurbs(jobs, **kw)
    finally:
        llm.use(previous)


class FakeSites:
    def __init__(self, table):
        self.table = table

    def resolve(self, name, apply_link="", website=""):
        return (self.table[name], "harvest") if name in self.table else ("", "no site found")


def _job(**kw):
    return {"title": "Director of IT", "company": "Dragos", "hiring_company": "Dragos",
            "apply_link": "https://boards.greenhouse.io/dragos/jobs/1", "score": 8,
            "description": "Lead corporate IT.", "rationale": OLD_BLURB + "\n" + NOTES, **kw}


def _sites(*_):
    return FakeSites({"Dragos": "https://dragos.com"})


ROUTES = {"https://dragos.com/": (200, _page(LONG)),
          "https://dragos.com/about": (200, _page(VALUES)),
          "https://dragos.com/careers": (200, _page(VALUES + "careers"))}


# --------------------------------------------------------------- resolution

def test_unnamed_staffing_client_is_not_profiled():
    assert profiles.profile_name({"company": "CyberCoders", "hiring_company": ""}) == ""
    assert profiles.profile_name({"company": "Jobleads-US", "hiring_company": "Dell Technologies"}) == \
        "Dell Technologies"
    assert profiles.profile_name({"company": "Dragos (via Swooped)"}) == "Dragos"


def _bare_sites(harvest=None, yaml_map=None):
    sites = object.__new__(profiles.Sites)
    sites._harvest = harvest
    sites._yaml = yaml_map or {}
    return sites


def test_site_resolution_order_and_own_site_only():
    sites = _bare_sites(lambda slug, name: {"Dragos": "www.dragos.com"}.get(name, ""),
                        {"nvidia": "https://www.nvidia.com/en-us/about-nvidia/careers/"})
    assert sites.resolve("Dragos") == ("https://www.dragos.com", "harvest")
    assert sites.resolve("NVIDIA") == ("https://www.nvidia.com", "companies.yaml")
    assert sites.resolve("Acme", "https://careers.acme.org/jobs/9") == ("https://careers.acme.org", "apply link")
    # an ATS, job board or LinkedIn apply link is never the company's site
    assert sites.resolve("Acme", "https://jobs.ashbyhq.com/acme/123") == ("", "no site found")
    assert sites.resolve("Acme", "https://www.linkedin.com/jobs/view/1") == ("", "no site found")
    assert profiles.site_domain("https://www.dragos.com") == "dragos.com"


def test_legal_suffix_is_tried_after_the_full_name():
    sites = _bare_sites(lambda slug, name: {"Caterpillar": "https://www.caterpillar.com"}.get(name, ""))
    assert sites.resolve("Caterpillar Inc.") == ("https://www.caterpillar.com", "harvest")
    assert profiles.name_variants("Padagis LLC") == ["Padagis LLC", "Padagis"]
    assert profiles.name_variants("Coralogix") == ["Coralogix"]


def test_a_cached_profile_for_another_company_is_not_used(tmp_path):
    path = tmp_path / "c.json"
    _enrich([_job()], cache_path=path, client=FakeClient(), today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    other = _job(company="Acme", hiring_company="Acme")
    client = FakeClient()
    _enrich([other], cache_path=path, client=client, today=TODAY,
                           sites=FakeSites({"Acme": "https://dragos.com"}),
                           http_factory=_factory(ROUTES))
    assert other["rationale"] == OLD_BLURB + "\n" + NOTES
    assert client.calls == []


# -------------------------------------------------------------------- fetch

def test_fetch_reads_homepage_and_first_two_subpages_with_text():
    with _factory({**ROUTES, "https://dragos.com/company": (200, _page(VALUES))})() as http:
        site = profiles.fetch_site(http, "https://dragos.com", deadline=1e12)
    assert site["status"] == "ok"
    assert [p["url"] for p in site["pages"]] == ["https://dragos.com/", "https://dragos.com/about",
                                                  "https://dragos.com/company"]
    assert "transparency" in site["text"]


def test_js_only_site_counts_as_nothing():
    routes = {"https://shell.io/": (200, '<html><body><div id="root"></div><script>x()</script></body></html>')}
    with _factory(routes)() as http:
        site = profiles.fetch_site(http, "https://shell.io", deadline=1e12)
    assert site["status"] == "no text" and site["text"] == ""


def test_robots_disallow_is_honoured():
    seen = []
    routes = {**ROUTES, "https://dragos.com/robots.txt": (200, "User-agent: *\nDisallow: /",
                                                          {"content-type": "text/plain"})}
    with _factory(routes, seen)() as http:
        site = profiles.fetch_site(http, "https://dragos.com", deadline=1e12)
    assert site["status"] == "robots"
    assert seen == ["https://dragos.com/robots.txt"]


def test_redirect_moves_the_paths_to_the_final_host():
    routes = {"https://dragos.com/": (301, "", {"location": "https://www.dragos.com/"}),
              "https://www.dragos.com/": (200, _page(LONG)),
              "https://www.dragos.com/about": (200, _page(VALUES))}
    with _factory(routes)() as http:
        site = profiles.fetch_site(http, "https://dragos.com", deadline=1e12)
    assert "https://www.dragos.com/about" in [p["url"] for p in site["pages"]]


# ---------------------------------------------------------------- the step

def test_rewrite_replaces_blurb_keeps_notes_and_score(tmp_path):
    job, client = _job(), FakeClient()
    stats = _enrich([job], cache_path=tmp_path / "c.json", client=client,
                                   today=TODAY, sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == NEW_BLURB + "\n" + NOTES
    assert job["score"] == 8
    assert client.calls == ["profile", "rewrite"]
    assert stats["rewritten"] == 1
    cached = json.loads((tmp_path / "c.json").read_text())
    assert cached["dragos.com"]["status"] == "ok"
    assert cached["dragos.com"]["profile"]["mission"] == "protect civilization"


def test_cache_hit_makes_no_request_and_no_profile_call(tmp_path):
    path = tmp_path / "c.json"
    _enrich([_job()], cache_path=path, client=FakeClient(), today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    seen, client = [], FakeClient()
    _enrich([_job(), _job(title="VP IT")], cache_path=path, client=client,
                           today=date(2026, 12, 1), sites=_sites(),
                           http_factory=_factory(ROUTES, seen))
    assert seen == []
    assert client.calls == ["rewrite", "rewrite"]


def test_expired_profile_is_fetched_again(tmp_path):
    path = tmp_path / "c.json"
    _enrich([_job()], cache_path=path, client=FakeClient(), today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    seen = []
    _enrich([_job()], cache_path=path, client=FakeClient(), today=date(2027, 2, 1),
                           sites=_sites(), http_factory=_factory(ROUTES, seen))
    assert "https://dragos.com/" in seen


def test_namesake_site_is_not_profiled_and_blurb_kept(tmp_path):
    job = _job()
    client = FakeClient(profile={"company_matches": False})
    _enrich([job], cache_path=tmp_path / "c.json", client=client, today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == OLD_BLURB + "\n" + NOTES
    assert client.calls == ["profile"]
    assert json.loads((tmp_path / "c.json").read_text())["dragos.com"]["status"] == "not this company"


def test_no_site_and_unnamed_employer_keep_the_blurb(tmp_path):
    jobs = [_job(company="Acme", hiring_company="Acme"), _job(company="CyberCoders", hiring_company="")]
    client = FakeClient()
    stats = _enrich(jobs, cache_path=tmp_path / "c.json", client=client, today=TODAY,
                                   sites=_sites(), http_factory=_factory(ROUTES))
    assert all(j["rationale"] == OLD_BLURB + "\n" + NOTES for j in jobs)
    assert (stats["no_site"], stats["no_employer"], client.calls) == (1, 1, [])


def test_api_failure_keeps_blurb_and_does_not_cache(tmp_path):
    job = _job()
    _enrich([job], cache_path=tmp_path / "c.json", client=FakeClient(fail=True),
                           today=TODAY, sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == OLD_BLURB + "\n" + NOTES
    assert not (tmp_path / "c.json").exists()


def test_fetch_timeout_is_not_cached(tmp_path):
    def boom(request):
        raise httpx.ConnectTimeout("slow")
    factory = lambda: httpx.Client(transport=httpx.MockTransport(boom))  # noqa: E731
    job = _job()
    _enrich([job], cache_path=tmp_path / "c.json", client=FakeClient(), today=TODAY,
                           sites=_sites(), http_factory=factory)
    assert job["rationale"] == OLD_BLURB + "\n" + NOTES
    assert not (tmp_path / "c.json").exists()


def test_over_cap_rewrite_keeps_the_old_blurb(tmp_path):
    job = _job()
    long = {"company": "word " * 30, "culture": "word " * 10}
    _enrich([job], cache_path=tmp_path / "c.json", client=FakeClient(rewrite=long),
                           today=TODAY, sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == OLD_BLURB + "\n" + NOTES


def test_spent_budget_keeps_blurbs(tmp_path):
    job, client = _job(), FakeClient()
    _enrich([job], cache_path=tmp_path / "c.json", client=client, today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES), budget_secs=1)
    assert job["rationale"] == OLD_BLURB + "\n" + NOTES
    assert client.calls == []


def test_rewrite_prompt_carries_the_del13_limits():
    prompt = " ".join(profiles.REWRITE_SYSTEM.split())
    assert "The role part of the blurb is not yours to change and is not shown to you" in prompt
    assert "Never what the product is or does" in prompt
    assert "Never mention compensation or pay" in prompt
    assert "never grade it" in prompt
    profile_prompt = " ".join(profiles.PROFILE_SYSTEM.split())
    assert "If the pages are all product or sales copy, \"culture\" is an empty list" in profile_prompt


# PM Hotel and Valon: the DEL-14 dry run's rewrite changed their role facts
# ("Likely top systems seat" -> "Senior systems seat"; Valon gained
# "Senior/Director"). The role is now copied by code, never by the model.
PM_HOTEL = ("Hotel management company, multi-property portfolio, hospitality. Likely top systems "
            "seat. Owns enterprise platforms: BI, accounting, procurement, expense; vendor "
            "contracts; small team. Culture: no signal in posting.")
PM_ROLE = ("Likely top systems seat. Owns enterprise platforms: BI, accounting, procurement, "
           "expense; vendor contracts; small team.")
VALON = ("Series C fintech, AI-native mortgage servicing OS, $110B+ loans. Leads Cloud + Developer "
         "Infrastructure org; owns compute, storage, CI/CD, observability, SRE — engineering-side "
         "infra, not enterprise IT. Culture: high-agency, direct feedback, technical depth.")
VALON_ROLE = ("Leads Cloud + Developer Infrastructure org; owns compute, storage, CI/CD, "
              "observability, SRE — engineering-side infra, not enterprise IT.")


@pytest.mark.parametrize("blurb, company, role, culture", [
    (PM_HOTEL, "Hotel management company, multi-property portfolio, hospitality.", PM_ROLE,
     "no signal in posting."),
    (VALON, "Series C fintech, AI-native mortgage servicing OS, $110B+ loans.", VALON_ROLE,
     "high-agency, direct feedback, technical depth."),
    ("U.S. Navy command; naval information warfare. Second-level supervisor. Owns systems "
     "engineering. Culture: no signal in posting.", "U.S. Navy command; naval information warfare.",
     "Second-level supervisor. Owns systems engineering.", "no signal in posting."),
])
def test_blurb_splits_into_company_role_culture(blurb, company, role, culture):
    assert profiles.split_blurb(blurb) == (company, role, culture)


def test_unsplittable_blurb_is_kept(tmp_path):
    job = _job(rationale="Old one-sentence rationale.")
    client = FakeClient()
    _enrich([job], cache_path=tmp_path / "c.json", client=client, today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == "Old one-sentence rationale."
    assert "rewrite" not in client.calls


@pytest.mark.parametrize("blurb, role", [(PM_HOTEL, PM_ROLE), (VALON, VALON_ROLE)])
def test_role_is_copied_verbatim_and_never_sent_to_the_rewrite(tmp_path, blurb, role):
    job = _job(rationale=blurb)
    client = FakeClient(rewrite={"company": "Top-15 hotel management company.",
                                 "role": "Senior systems seat; Senior/Director.",
                                 "culture": "people-first, transparent"})
    _enrich([job], cache_path=tmp_path / "c.json", client=client, today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"] == f"Top-15 hotel management company. {role} Culture: people-first, transparent."
    sent = [content for kind, content in client.requests if kind == "rewrite"]
    assert sent and role not in sent[0]
    budget = min(18, profiles.BLURB_TARGET_WORDS - len(role.split()) - 1)
    assert f"Word budget for company + culture together: {budget}" in sent[0]


def test_product_only_site_gives_no_culture_signal(tmp_path):
    """Coralogix: a site of product copy has no culture keywords, and the
    rewrite returns none; the blurb says so in the canonical words."""
    coralogix = ("AI-powered observability/log-analytics SaaS platform for engineering teams. Top IS "
                 "seat. Owns enterprise apps (NetSuite, Salesforce, Workato), integrations, data "
                 "platform, automation/AI, vendor budget, governance. Culture: no signal in posting.")
    job = _job(rationale=coralogix)
    client = FakeClient(profile={"company_matches": True, "what": "AI-native observability SaaS",
                                 "who_for": "engineering teams", "size": "4,000+ customers",
                                 "mission": None, "culture": []},
                        rewrite={"company": "AI-native observability SaaS; 4,000+ teams.", "culture": ""})
    _enrich([job], cache_path=tmp_path / "c.json", client=client, today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES))
    assert job["rationale"].endswith("governance. Culture: no signal in posting or site.")
    sent = [content for kind, content in client.requests if kind == "rewrite"][0]
    assert 'Website culture keywords: []' in sent


def test_profile_keeps_culture_apart_from_product_facts():
    client = FakeClient(profile={"company_matches": True, "what": "observability SaaS",
                                 "who_for": None, "size": None, "mission": None,
                                 "culture": ["remote-first", " ", "transparency"],
                                 "operates": ["AI-first", "cloud-native"]})
    llm.use(client)
    try:
        profile = profiles.summarise(profiles.Calls(), "Coralogix", {"text": "x"})
    finally:
        llm.use(llm_stub.Unstubbed())
    assert profile == {"what": "observability SaaS", "who_for": None, "size": None,
                       "mission": None, "culture": ["remote-first", "transparency"]}


def test_a_profile_from_an_older_version_is_fetched_again(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"dragos.com": {
        "company": "Dragos", "checked": TODAY.isoformat(), "status": "ok", "pages": [],
        "profile": {"what": "x", "operates": ["AI-first"]}}}))
    seen = []
    _enrich([_job()], cache_path=path, client=FakeClient(), today=TODAY,
                           sites=_sites(), http_factory=_factory(ROUTES, seen))
    assert "https://dragos.com/" in seen
    assert json.loads(path.read_text())["dragos.com"]["version"] == profiles.PROFILE_VERSION


# ------------------------------------------------------------- the digest

from test_fil10_cross_source_dedup import _job as _queued, pipeline  # noqa: E402,F401  isort: skip
import main  # noqa: E402
import pending  # noqa: E402

LINK = "https://boards.greenhouse.io/dragos/jobs/5399442008"


def _digest_with(pipeline, monkeypatch, enrich):
    calls, _ = pipeline
    monkeypatch.setattr(main, "get_recent_index_rows", lambda days: [])
    monkeypatch.setattr(profiles, "enrich_blurbs", enrich)
    from datetime import datetime
    pending.write_batch([_queued(LINK, "Director of IT", "Dragos", "ATS", score=8,
                                 rationale="posting-only blurb")], "t",
                        found_at=datetime.utcnow())
    main.digest_run()
    return calls


def test_digest_card_and_sheet_row_carry_the_rewritten_blurb(pipeline, monkeypatch):
    def enrich(cards, **kw):
        for c in cards:
            c["rationale"], c["profile_domain"] = "site-informed blurb", "dragos.com"
    calls = _digest_with(pipeline, monkeypatch, enrich)
    assert [c["rationale"] for c in calls["emails"][-1]["main"]] == ["site-informed blurb"]
    assert [r["rationale"] for r in calls["sheet"]] == ["site-informed blurb"]
    assert calls["sheet"][0]["score"] == 8


def test_a_failing_profile_step_never_blocks_the_digest(pipeline, monkeypatch):
    def enrich(cards, **kw):
        raise RuntimeError("boom")
    calls = _digest_with(pipeline, monkeypatch, enrich)
    assert len(calls["emails"]) == 1
    assert [r["rationale"] for r in calls["sheet"]] == ["posting-only blurb"]
