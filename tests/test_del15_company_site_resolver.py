"""DEL-15: the scoring call names the hiring company's website domain; the
profile step uses it only after the harvest, companies.yaml and the apply link
fail, and only when the fetched homepage names the company.

Covers: the prompt asks for the field and the reply is cut to a bare domain;
the field never changes the score; resolution order (the scoring-call domain
is last, and never an ATS / job board / LinkedIn host); a homepage that names
the company (title or visible text, punctuation and legal suffix ignored) is
read and profiled; one that does not is rejected with one log line, no
subpage request, no profile call, the blurb kept; an unnamed staffing client
stays skipped; a harvest site is never put through the name check. No network.
"""

import json

import pytest

from test_del14_company_profiles import (  # noqa: F401  isort: skip
    LONG, OLD_BLURB, TODAY, VALUES, FakeClient, _bare_sites, _enrich, _factory, _job, _page)
import llm  # noqa: E402
import profiles  # noqa: E402
import scorer  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402


def _home(title, text):
    return f"<html><head><title>{title}</title></head><body><main><p>{text}</p></main></body></html>"


class ModelSites:
    """Only the scoring-call domain resolves, as for a company the harvest,
    companies.yaml and the apply link all miss."""

    def resolve(self, name, apply_link="", website=""):
        return (profiles.site_origin(website), profiles.SCORING_CALL) if website else ("", "no site found")


def _run(job, routes, tmp_path, seen=None, client=None):
    client = client or FakeClient()
    stats = _enrich([job], cache_path=tmp_path / "c.json", client=client,
                                   today=TODAY, sites=ModelSites(),
                                   http_factory=_factory(routes, seen))
    return stats, client


# ------------------------------------------------------------- scoring call

def test_prompt_asks_for_the_website_and_keeps_it_out_of_the_score():
    prompt = " ".join(scorer.SYSTEM_PROMPT.split())
    assert '"website": "<the hiring company\'s bare domain, or null>"' in prompt
    assert "This field never affects the score." in prompt
    assert scorer.MAX_TOKENS == 1000


@pytest.mark.parametrize("raw, domain", [
    ("dragos.com", "dragos.com"),
    ("https://www.Dragos.com/about", "dragos.com"),
    ("www.usda.gov", "usda.gov"),
    ("klgates.com/", "klgates.com"),
    (None, ""), ("null", ""), ("", ""),
    ("Dragos Inc", ""), ("localhost", ""),
])
def test_reply_website_is_cut_to_a_bare_domain(raw, domain):
    assert scorer.website_domain(raw) == domain


def test_website_is_stored_on_the_job_and_never_moves_the_score(monkeypatch):
    def reply(website):
        body = {"score": 7, "rationale": "x", "is_insurance": False,
                "is_cool_company_wrong_role": False, "function_fit": "leads_department",
                "work_model": "Remote", "city": None, "state": None,
                "hiring_company": "Dragos", "website": website}
        return StubProvider(lambda call: json.dumps(body))
    job = {"title": "Director of IT", "company": "Dragos", "description": "Lead IT."}
    monkeypatch.setattr(llm, "_provider", reply("https://www.dragos.com/"))
    with_site = scorer._score_single(job)
    monkeypatch.setattr(llm, "_provider", reply(None))
    without = scorer._score_single(job)
    assert with_site["website"] == "dragos.com" and without["website"] == ""
    assert with_site["score"] == without["score"] == 7


# --------------------------------------------------------------- resolution

def test_scoring_call_domain_is_tried_last_and_only_when_it_is_an_own_site():
    sites = _bare_sites(lambda slug, name: {"Dragos": "www.dragos.com"}.get(name, ""))
    # the harvest wins over the scoring call
    assert sites.resolve("Dragos", "", "dragos-fake.com") == ("https://www.dragos.com", "harvest")
    # so does an own-site apply link
    assert sites.resolve("Acme", "https://careers.acme.org/jobs/9", "acme.com") == \
        ("https://careers.acme.org", "apply link")
    # an ATS apply link falls through to the scoring call
    assert sites.resolve("Acme", "https://jobs.ashbyhq.com/acme/1", "acme.com") == \
        ("https://acme.com", profiles.SCORING_CALL)
    # never an ATS, job board or LinkedIn host
    assert sites.resolve("Acme", "", "boards.greenhouse.io") == ("", "no site found")
    assert sites.resolve("Acme", "", "linkedin.com") == ("", "no site found")


@pytest.mark.parametrize("name, title, text, ok", [
    ("NXP Semiconductors", "NXP Semiconductors | Automotive, IoT", LONG, True),
    ("K&L Gates", "Home", "K&amp;L Gates is a global law firm. " + LONG, True),
    ("Taiho Oncology, Inc.", "Taiho Oncology", LONG, True),
    ("Yahoo", "Yahoo | Mail, Weather, Search", LONG, True),
    ("Google", "Example Domain", LONG, False),
    ("BAE Systems", "BAE Inc. parked page", LONG, False),
    ("Gates", "K&L Gates", LONG, True),
    ("Nestlé", "Nestle Global", LONG, False),
])
def test_homepage_names_the_company(name, title, text, ok):
    html = _home(title, text)
    from sources.bespoke import visible_text
    assert profiles.homepage_names(name, html, visible_text(html)) is ok


# ------------------------------------------------------------------- the step

def test_named_homepage_is_read_and_profiled(tmp_path):
    routes = {"https://dragos.com/": (200, _home("Dragos | Industrial Cybersecurity", LONG)),
              "https://dragos.com/about": (200, _page(VALUES))}
    stats, client = _run(_job(website="dragos.com"), routes, tmp_path)
    assert stats["model_accepted"] == 1 and stats["model_rejected"] == 0
    assert stats["sites"]["dragos.com"]["method"] == profiles.SCORING_CALL
    assert stats["sites"]["dragos.com"]["pages"] == 2
    assert client.calls == ["profile", "rewrite"] and stats["rewritten"] == 1


def test_homepage_without_the_name_is_rejected_and_nothing_else_is_read(tmp_path, capsys):
    routes = {"https://dragos.com/": (200, _home("Welcome", LONG)),
              "https://dragos.com/about": (200, _page(VALUES))}
    seen = []
    job = _job(website="dragos.com")
    stats, client = _run(job, routes, tmp_path, seen)
    assert stats["model_rejected"] == 1 and stats["model_accepted"] == 0
    assert stats["sites"]["dragos.com"]["status"] == profiles.NAME_NOT_ON_HOMEPAGE
    assert not any(u.endswith("/about") for u in seen)
    assert client.calls == [] and job["rationale"].startswith(OLD_BLURB)
    out = capsys.readouterr().out
    assert out.count("scoring-call domain dragos.com rejected") == 1
    # cached as an empty site, same TTL as the other empty statuses
    assert json.loads((tmp_path / "c.json").read_text())["dragos.com"]["status"] == \
        profiles.NAME_NOT_ON_HOMEPAGE


def test_unreadable_homepage_reads_no_subpage(tmp_path):
    routes = {"https://dragos.com/": (403, ""),
              "https://dragos.com/about": (200, _page("Dragos " + VALUES))}
    seen = []
    stats, client = _run(_job(website="dragos.com"), routes, tmp_path, seen)
    assert stats["sites"]["dragos.com"]["status"] == "blocked"
    assert not any(u.endswith("/about") for u in seen) and client.calls == []


def test_unnamed_staffing_client_stays_skipped_with_a_website(tmp_path):
    seen = []
    stats, _ = _run(_job(company="CyberCoders", hiring_company="", website="cybercoders.com"),
                    {}, tmp_path, seen)
    assert stats["no_employer"] == 1 and seen == []


def test_harvest_site_is_not_name_checked(tmp_path):
    from test_del14_company_profiles import _sites
    routes = {"https://dragos.com/": (200, _home("Welcome", LONG)),
              "https://dragos.com/about": (200, _page(VALUES))}
    stats = _enrich([_job(website="other.com")], cache_path=tmp_path / "c.json",
                                   client=FakeClient(), today=TODAY, sites=_sites(),
                                   http_factory=_factory(routes))
    assert stats["sites"]["dragos.com"]["status"] == "ok" and stats["model_rejected"] == 0


# ------------------------------------------------- follow-up: leading words

@pytest.mark.parametrize("name, leads", [
    ("RWE Americas, LLC", ["RWE"]),
    ("Planned Parenthood Federation of America", ["Planned Parenthood"]),
    ("Nestlé IT & Digital Americas", ["Nestlé"]),
    ("Kraft Heinz Company", ["Kraft Heinz"]),
    ("Bank of America", []),            # "of America" is the name, not a qualifier
    ("Standard Bank Egypt", []),        # a country is never stripped
    ("United States", []),
    ("Google", []),
])
def test_leading_words(name, leads):
    assert profiles.leading_words(name) == leads


@pytest.mark.parametrize("name, title, ok", [
    ("RWE Americas, LLC", "RWE | Energy for a sustainable life", True),
    ("Planned Parenthood Federation of America", "Planned Parenthood", True),
    ("Bank of America", "Bank of the West", False),
    ("Standard Bank Egypt", "Standard Bank Group", False),
])
def test_homepage_naming_the_leading_words_is_accepted(name, title, ok):
    html = _home(title, LONG)
    from sources.bespoke import visible_text
    assert profiles.homepage_names(name, html, visible_text(html)) is ok


# ------------------------------------------------- follow-up: sources tried

class ChainSites:
    """A harvest site, then the scoring call's domain when the job has one."""

    def candidates(self, name, apply_link="", website=""):
        out = [("https://dragos-old.com", "harvest")]
        if website:
            out.append((profiles.site_origin(website), profiles.SCORING_CALL))
        return out


GOOD = {"https://dragos.com/": (200, _home("Dragos | Industrial Cybersecurity", LONG)),
        "https://dragos.com/about": (200, _page(VALUES))}


def _chain_run(job, cache, tmp_path, routes, seen):
    path = tmp_path / "c.json"
    if cache is not None:
        path.write_text(json.dumps(cache))
    client = FakeClient()
    stats = _enrich([job], cache_path=path, client=client, today=TODAY,
                                   sites=ChainSites(), http_factory=_factory(routes, seen))
    return stats, client, json.loads(path.read_text()) if path.exists() else {}


def _held(tried=None):
    entry = {"company": "Dragos", "method": "harvest", "checked": TODAY.isoformat(),
             "version": profiles.PROFILE_VERSION, "status": "blocked", "pages": [], "profile": None}
    if tried is not None:
        entry["tried"] = tried
    return {"dragos-old.com": entry}


def test_failed_site_falls_through_to_the_next_source(tmp_path):
    seen = []
    routes = {"https://dragos-old.com/": (403, ""), **GOOD}
    stats, client, cache = _chain_run(_job(website="dragos.com"), None, tmp_path, routes, seen)
    assert stats["rewritten"] == 1 and stats["model_accepted"] == 1
    assert cache["dragos-old.com"]["tried"] == ["harvest"]
    assert cache["dragos.com"]["tried"] == ["harvest", profiles.SCORING_CALL]


def test_hold_is_honoured_when_every_source_now_was_tried(tmp_path):
    seen = []
    stats, client, _ = _chain_run(_job(website="dragos.com"),
                                  _held(["harvest", profiles.SCORING_CALL]), tmp_path, GOOD, seen)
    assert seen == [] and client.calls == [] and stats["rewritten"] == 0


def test_hold_is_skipped_when_a_new_source_is_available(tmp_path, capsys):
    seen = []
    stats, _, cache = _chain_run(_job(website="dragos.com"), _held(["harvest"]), tmp_path, GOOD, seen)
    assert not any("dragos-old.com" in u for u in seen)      # the held site is not refetched
    assert stats["rewritten"] == 1 and "dragos.com" in cache
    assert "scoring call not tried then" in capsys.readouterr().out


def test_hold_without_a_source_list_is_re_resolved_once(tmp_path):
    seen = []
    routes = {"https://dragos-old.com/": (403, ""), **GOOD}
    stats, _, cache = _chain_run(_job(website="dragos.com"), _held(), tmp_path, routes, seen)
    assert "https://dragos-old.com/" in seen and stats["rewritten"] == 1
    assert cache["dragos-old.com"]["tried"] == ["harvest"]


def test_hold_without_a_source_list_and_nothing_new_records_its_sources(tmp_path):
    seen = []
    routes = {"https://dragos-old.com/": (403, "")}
    _, _, cache = _chain_run(_job(), _held(), tmp_path, routes, seen)
    assert seen and cache["dragos-old.com"]["tried"] == ["harvest"]
    seen.clear()
    _chain_run(_job(), None, tmp_path, routes, seen)           # next digest: held
    assert seen == []


def test_whole_chain_failing_is_held_next_time(tmp_path):
    seen = []
    routes = {"https://dragos-old.com/": (403, ""),
              "https://dragos.com/": (200, _home("Welcome", LONG))}
    _, _, cache = _chain_run(_job(website="dragos.com"), None, tmp_path, routes, seen)
    assert cache["dragos-old.com"]["tried"] == ["harvest", profiles.SCORING_CALL]
    assert cache["dragos.com"]["status"] == profiles.NAME_NOT_ON_HOMEPAGE
    seen.clear()
    _, client, _ = _chain_run(_job(website="dragos.com"), None, tmp_path, routes, seen)
    assert seen == [] and client.calls == []
