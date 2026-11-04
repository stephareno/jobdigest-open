"""ATS slug resolver for the dream-company watchlist.

One-time (re-runnable) tool. Reads company names from data/dream_companies.txt,
generates slug candidates for each, and probes the public JSON APIs of four ATS
providers (Greenhouse, Lever, Ashby, SmartRecruiters). The first candidate that
returns HTTP 200 with a non-empty job array wins. Results are written to
data/companies.yaml, which sources/ats.py consumes for the daily fetch.

Slugs derived from the company's full name (no-spaces, hyphenated, or legal-
suffix-stripped) are trusted on a hit. Bare first-word candidates ("blue" for
"Blue Origin") are risky — an unrelated company may own the slug — so a
first-word hit is only accepted if the company's distinctive name phrase
appears in the returned job text; otherwise it is rejected as an impostor.

Resume-safe: companies already resolved in data/companies.yaml are skipped on
re-run unless --force is given. Previously-unresolved companies are always
retried. Entries hand-marked `pinned: true` in companies.yaml are never
re-resolved, even with --force — use this for boards verified by a human that
the automatic phrase check cannot confirm. Workday tenants are out of scope
and will land in the unresolved list; the hand-maintained Workday tenants and
careers-page URLs live in the top-level `pinned:` section (PUB-04), which this
tool never reads or changes and writes back exactly as it found it.

Usage:
    python3 tools/resolve_ats.py [--force] [--input PATH] [--output PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import random
import re
import sys
import urllib.robotparser
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx
import yaml

log = logging.getLogger("resolve_ats")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "data" / "dream_companies.txt"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "companies.yaml"

USER_AGENT = "job-digest-agent/1.0"
REQUEST_TIMEOUT = 10.0
MAX_IN_FLIGHT = 8
HOST_DELAY_SECONDS = 0.1  # courtesy delay between requests to the same host

# Trailing words treated as legal/decorative suffixes when generating slug
# candidates ("Varda Space Industries" -> "varda space", "Mistral AI" -> "mistral").
LEGAL_SUFFIXES = {
    "inc", "llc", "corp", "corporation", "co", "company",
    "labs", "lab", "technologies", "technology", "industries",
    "systems", "pbc", "ai",
    # Non-US legal forms; stripping these only adds slug candidates and keeps
    # the impostor check's name phrase to the part a job board actually prints.
    "ltd", "limited", "plc", "gmbh", "ag", "nv", "bv", "srl", "spa", "oy", "ab",
}


def _greenhouse_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("jobs"))


def _lever_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


def _ashby_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("jobs"))


def _smartrecruiters_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("content"))


def _rippling_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


def _workable_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("jobs"))


def _recruitee_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("offers"))


def _bamboohr_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("result"))


def _breezy_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


def _teamtailor_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("items"))


def _personio_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


# ATS name -> (URL template, validator for "200 with a non-empty job array").
# Order matters: on a multi-ATS hit for the same candidate, the first listed wins.
ATS_PROBES: dict[str, tuple[str, Callable[[Any], bool]]] = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        _greenhouse_has_jobs,
    ),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}?mode=json",
        _lever_has_jobs,
    ),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        _ashby_has_jobs,
    ),
    "smartrecruiters": (
        "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        _smartrecruiters_has_jobs,
    ),
    # Documented public board API; no auth. Returns a bare job array like Lever
    # and 404s cleanly on an unknown slug.
    "rippling": (
        "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs",
        _rippling_has_jobs,
    ),
    # --- providers added Aug 2026; every endpoint verified against a live board ---
    "workable": (
        "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
        _workable_has_jobs,
    ),
    "recruitee": (
        "https://{slug}.recruitee.com/api/offers/",
        _recruitee_has_jobs,
    ),
    "bamboohr": (
        "https://{slug}.bamboohr.com/careers/list",
        _bamboohr_has_jobs,
    ),
    "breezy": (
        "https://{slug}.breezy.hr/json",
        _breezy_has_jobs,
    ),
    "teamtailor": (
        "https://{slug}.teamtailor.com/jobs.json",
        _teamtailor_has_jobs,
    ),
    "personio": (
        "https://{slug}.jobs.personio.de/search.json",
        _personio_has_jobs,
    ),
}

# Providers whose board token cannot be derived from a company name — it is a
# host plus a site code, or a tenant plus a GUID. Slug guessing can never find
# these; only careers-page detection can, so they live outside ATS_PROBES.
# Oracle's endpoint is verified; Workday has no uniform public API at all.
ORACLE_HCM_TEMPLATE = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},limit=200"
)


def _oracle_hcm_has_jobs(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    for item in data.get("items") or []:
        if item.get("requisitionList"):
            return True
    return False


# --- page-only providers added Sep 2026 (SRC-13) ------------------------------
# Every endpoint below was verified against a live board on 2026-09-16. None
# is slug-guessable from a company name at a cost worth paying (ADP and UKG
# Ready tokens are opaque ids, Comeet needs a uid plus a token), so like Oracle
# they are found only on careers pages and stay out of ATS_PROBES -- stage 1
# would otherwise fan out to them for every company.

def _adp_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("jobRequisitions"))


def _ukg_ready_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("job_requisitions"))


def _gem_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


def _comeet_has_jobs(data: Any) -> bool:
    return isinstance(data, list) and len(data) > 0


def _manatal_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("results"))


def _jobscore_has_jobs(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("jobs"))


# ADP Workforce Now: the token is the career-center cid (a UUID).
ADP_WFN_TEMPLATE = (
    "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
    "job-requisitions?cid={slug}&timeStamp=0&lang=en_US&locale=en_US&$top=100"
)
# UKG Ready: token is "host:companyId", e.g. "secure7.saashr.com:6213629".
UKG_READY_TEMPLATE = (
    "https://{host}/ta/rest/ui/recruitment/companies/%7C{company_id}/job-requisitions"
    "?offset=0&size=100&sort=desc&lang=en-US"
)
# Comeet: token is "companyUid:apiToken"; both are printed on the careers page.
COMEET_TEMPLATE = (
    "https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}&details=true"
)
# A Teamtailor career site on the company's own domain serves the same JSON
# feed as <slug>.teamtailor.com; the token is then the full host.
TEAMTAILOR_SITE_TEMPLATE = "https://{host}/jobs.json"

PAGE_ONLY_PROBES: dict[str, tuple[str, Callable[[Any], bool]]] = {
    "adp": (ADP_WFN_TEMPLATE, _adp_has_jobs),
    "ukg_ready": (UKG_READY_TEMPLATE, _ukg_ready_has_jobs),
    "gem": ("https://api.gem.com/job_board/v0/{slug}/job_posts/", _gem_has_jobs),
    "comeet": (COMEET_TEMPLATE, _comeet_has_jobs),
    # careers-page.com is Manatal's domain (it was once filed as a Recruitee
    # white-label; the Recruitee API 404s for these slugs, Manatal's answers).
    "manatal": ("https://core.api.manatal.com/open/v3/career-page/{slug}/jobs/?page_size=100",
                _manatal_has_jobs),
    "jobscore": ("https://careers.jobscore.com/jobs/{slug}/feed.json", _jobscore_has_jobs),
}

# Vendors whose token is an opaque id that never spells the company's name, and
# whose payload does not reliably name the company either. See board_belongs_to.
OPAQUE_TOKEN_ATS = {"adp", "ukg_ready"}


# Slugs this short are treated as risky regardless of how they were derived.
SHORT_SLUG_CHARS = 4


class SlugCandidate(NamedTuple):
    """A slug to probe, plus whether a hit needs name-phrase verification."""

    slug: str
    needs_verification: bool


def _name_words(name: str) -> list[str]:
    cleaned = re.sub(r"[^a-z0-9\s-]", "", name.lower().replace("&", " and ").strip())
    return cleaned.replace("-", " ").split()


def name_phrase(name: str) -> str:
    """The company's distinctive name phrase: normalized, legal suffixes stripped."""
    words = _name_words(name)
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def slug_candidates(name: str) -> list[SlugCandidate]:
    """Generate ordered, deduplicated slug candidates for a company name.

    Variants: no-spaces, hyphenated, legal-suffix-stripped versions of both,
    and first word only. Full-name-derived variants are trusted on a hit;
    the bare first-word variant is marked as needing verification because an
    unrelated company may own that slug.
    """
    words = _name_words(name)
    if not words:
        return []

    candidates: list[SlugCandidate] = []

    def add_variants(parts: list[str], needs_verification: bool) -> None:
        candidates.append(SlugCandidate("".join(parts), needs_verification))
        if len(parts) > 1:
            candidates.append(SlugCandidate("-".join(parts), needs_verification))

    add_variants(words, needs_verification=False)
    trimmed = words
    while len(trimmed) > 1 and trimmed[-1] in LEGAL_SUFFIXES:
        trimmed = trimmed[:-1]
        add_variants(trimmed, needs_verification=False)
    candidates.append(SlugCandidate(words[0], needs_verification=True))

    seen: set[str] = set()
    out: list[SlugCandidate] = []
    for cand in candidates:
        if cand.slug and cand.slug not in seen:
            seen.add(cand.slug)
            # A very short slug is a coincidence waiting to happen even when it
            # is the company's whole name: "DCS Corp" -> "dcs" matched
            # dc Services GmbH, and "Pact" -> "pact" matched an unrelated
            # infrastructure firm. Short slugs must prove themselves.
            if len(cand.slug) <= SHORT_SLUG_CHARS:
                cand = cand._replace(needs_verification=True)
            out.append(cand)
    return out


# Vendors hosting many small tenants in one flat namespace, where a common
# word is usually someone else's board. Observed 2026-09-16: "Teleport" hit a
# German telecom on Personio, "Hilton" a small Breezy board, "Google" and
# "Samsung" Recruitee sample boards, "Hadrian" a Dutch security firm on
# Teamtailor. The content check cannot catch these for a one-word name: the
# name is the slug, and every payload prints its own slug or subdomain.
# Vendors where a one-word slug guess cannot be trusted: the namespace is shared,
# so the slug may be another company's tenant. BambooHR joins them (SRC-18): of
# the 8 one-word guesses it produced, 3 served BambooHR's shared demo board
# (byte-identical payloads for bizcap, worthy and baskhealth) and the rest were
# another employer or unverifiable. Those companies fall through to the
# careers-page stage, which takes a board only if their own site links to it.
AMBIGUOUS_SLUG_ATS = {"recruitee", "breezy", "teamtailor", "personio", "workable", "bamboohr"}


def slug_hit_accepted(name: str, ats: str, candidate: SlugCandidate, data: Any) -> bool:
    """Whether a stage-1 slug hit may resolve the company.

    A one-word name on an ambiguous vendor is never accepted from a guess; the
    company falls through to the careers-page stage, where a board counts only
    if the company's own confirmed site links to it. Everything else follows
    the original rule: risky candidates must pass the content check.
    """
    if ats in AMBIGUOUS_SLUG_ATS and len(name_phrase(name).split()) == 1:
        return False
    return not candidate.needs_verification or board_matches_company(name, ats, data)


def _normalize_text(text: str) -> str:
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def board_matches_company(name: str, ats: str, data: Any) -> bool:
    """Check that a board actually belongs to the company, not a slug squatter.

    Looks for the company's distinctive name phrase in the titles, descriptions,
    and (where the ATS provides it) company name of the first few postings.
    """
    parts: list[str] = []
    if ats == "greenhouse":
        for job in (data.get("jobs") or [])[:5]:
            parts += [job.get("title", ""), str(job.get("content", ""))]
    elif ats == "ashby":
        for job in (data.get("jobs") or [])[:5]:
            parts += [job.get("title", ""), job.get("descriptionPlain", "") or str(job.get("descriptionHtml", ""))]
    elif ats == "lever":
        for job in (data or [])[:5]:
            parts += [job.get("text", ""), str(job.get("description", "")), str(job.get("additional", ""))]
    elif ats == "smartrecruiters":
        for job in (data.get("content") or [])[:5]:
            parts += [job.get("name", ""), (job.get("company") or {}).get("name", "")]
    elif ats == "rippling":
        for job in (data or [])[:5]:
            parts += [job.get("name", ""), (job.get("department") or {}).get("label", ""),
                      str(job.get("url", ""))]
    elif ats == "workable":
        for job in (data.get("jobs") or [])[:5]:
            parts += [job.get("title", ""), str(job.get("description", "")), str(job.get("url", ""))]
    elif ats == "recruitee":
        for job in (data.get("offers") or [])[:5]:
            parts += [job.get("title", ""), job.get("company_name", ""), str(job.get("description", ""))]
    elif ats == "bamboohr":
        for job in (data.get("result") or [])[:5]:
            parts += [job.get("jobOpeningName", ""), job.get("departmentLabel", "")]
    elif ats == "breezy":
        for job in (data or [])[:5]:
            parts += [job.get("name", ""), (job.get("company") or {}).get("name", ""), str(job.get("url", ""))]
    elif ats == "teamtailor":
        for job in (data.get("items") or [])[:5]:
            parts += [job.get("title", ""), str(job.get("url", "")), str(job.get("content_html", ""))[:2000]]
    elif ats == "oracle_hcm":
        # Oracle nests postings under items[].requisitionList[]; without this
        # branch parts stayed empty and every Oracle board failed the check.
        for item in (data.get("items") or [])[:2]:
            for job in (item.get("requisitionList") or [])[:5]:
                parts += [job.get("Title", ""), str(job.get("ShortDescriptionStr", "")),
                          str(job.get("JobFamily") or ""), str(job.get("Organization") or "")]
    elif ats == "personio":
        for job in (data or [])[:5]:
            parts += [job.get("name", ""), job.get("department", ""), job.get("subcompany", "")]
    elif ats == "adp":
        for job in (data.get("jobRequisitions") or [])[:5]:
            parts += [job.get("requisitionTitle", "")]
    elif ats == "ukg_ready":
        for job in (data.get("job_requisitions") or [])[:5]:
            parts += [job.get("job_title", ""), str(job.get("job_description", ""))[:3000],
                      job.get("reapply_msg", "")]
    elif ats == "gem":
        for job in (data or [])[:5]:
            parts += [job.get("title", ""), str(job.get("content_plain") or job.get("content", ""))[:3000],
                      str(job.get("absolute_url", ""))]
    elif ats == "comeet":
        for job in (data or [])[:5]:
            parts += [job.get("name", ""), job.get("company_name", ""),
                      str(job.get("url_active_page", ""))]
    elif ats == "manatal":
        for job in (data.get("results") or [])[:5]:
            parts += [job.get("position_name", ""), str(job.get("description", ""))[:3000]]
    elif ats == "jobscore":
        parts += [str(data.get("company_name", "")), str(data.get("company_url", ""))]
        for job in (data.get("jobs") or [])[:5]:
            parts += [job.get("title", ""), str(job.get("description", ""))[:3000]]
    # Providers return null for optional fields (company_name, department,
    # description), so coerce rather than assuming every part is a string.
    return name_phrase(name) in _normalize_text(" ".join(str(p) for p in parts if p))


# Public suffixes needing three labels to identify one owner, so that e.g.
# two unrelated .co.uk sites are not throttled against each other.
MULTIPART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
    "co.jp", "co.nz", "co.za", "com.br", "com.mx", "co.in", "com.sg",
}

# Vendors that rate-limit aggressively across their whole tenant estate.
# Personio returns 429 for concurrent requests to *different* tenants.
# Recruitee rate-limits across every tenant subdomain: at the 0.1 s default,
# ~7% of SRC-17's probes came back 429 (21 Sep).
# Workable refuses after a few dozen requests at the default: PUB-12's
# pre-resolve of the public list got 429 on nearly every probe (29 Oct), so it
# is paced at 2-4 s a request (the delay plus up to as much again in jitter).
# `main.py resolve` paces Workable with its own queue instead (resolve.py,
# WorkablePacer) and passes overrides without it.
DOMAIN_DELAY_OVERRIDES = {"personio.de": 0.6, "personio.com": 0.6, "recruitee.com": 0.6,
                          "workable.com": 2.0}
RATE_LIMIT_WAIT_CAP = 10.0  # most seconds a 429's Retry-After is honoured before the one retry


def throttle_key(host: str) -> str:
    """The registrable domain of a host — the unit a vendor rate-limits on.

    Per-tenant subdomain boards (acme.bamboohr.com, acme.jobs.personio.de) are
    all one owner's infrastructure. Keying the throttle on the full host gave
    every tenant its own bucket, which meant no throttling at all against
    vendors that host thousands of boards.
    """
    labels = host.lower().split(".")
    if len(labels) < 3:
        return host.lower()
    if ".".join(labels[-2:]) in MULTIPART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class HostThrottle:
    """Serializes requests per registrable domain with a courtesy delay plus jitter."""

    def __init__(self, base_delay: float = HOST_DELAY_SECONDS,
                 overrides: dict[str, float] | None = None) -> None:
        self._base_delay = base_delay
        self._overrides = DOMAIN_DELAY_OVERRIDES if overrides is None else overrides
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_ok: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        key = throttle_key(host)
        delay_for_key = max(self._base_delay, self._overrides.get(key, 0.0))
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            delay = self._next_ok.get(key, 0.0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            jitter = random.uniform(0, delay_for_key)
            self._next_ok[key] = loop.time() + delay_for_key + jitter


async def probe(
    client: httpx.AsyncClient,
    url: str,
    validator: Callable[[Any], bool],
    throttle: HostThrottle,
    semaphore: asyncio.Semaphore,
) -> Any | None:
    """Return the parsed JSON body if the URL responds 200 with a non-empty
    job array, else None.

    404 means "not this ATS" and is never retried. 5xx, 429 and timeouts get
    exactly one retry -- a 429 after its Retry-After, capped. A 429 used to
    count as a miss, which silently lost boards on a vendor throttling us.
    Any other error counts as a miss.
    """
    host = urlsplit(url).netloc
    for attempt in (1, 2):
        async with semaphore:
            await throttle.wait(host)
            try:
                response = await client.get(url)
            except httpx.TimeoutException:
                if attempt == 1:
                    log.debug("timeout, retrying once: %s", url)
                    continue
                log.warning("timed out twice, giving up: %s", url)
                return None
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # InvalidURL is not an HTTPError: a Crunchbase slug like "data²"
                # makes an invalid IDNA host, and uncaught it killed a whole
                # 2,500-company resolver batch (SRC-12 crawl, 16 Sep).
                log.warning("request error (no retry) for %s: %s", url, exc)
                return None

        if response.status_code == 404:
            return None
        if response.status_code == 429:
            if attempt == 1:
                try:
                    wait_s = float(response.headers.get("retry-after") or 2.0)
                except ValueError:
                    wait_s = 2.0
                await asyncio.sleep(min(max(wait_s, 0.5), RATE_LIMIT_WAIT_CAP))
                continue
            log.warning("HTTP 429 twice, giving up: %s", url)
            return None
        if response.status_code >= 500:
            if attempt == 1:
                log.debug("HTTP %s, retrying once: %s", response.status_code, url)
                continue
            log.warning("HTTP %s twice, giving up: %s", response.status_code, url)
            return None
        if response.status_code != 200:
            log.debug("HTTP %s treated as miss: %s", response.status_code, url)
            return None

        try:
            data = response.json()
        except ValueError:
            log.debug("non-JSON 200 treated as miss: %s", url)
            return None
        return data if validator(data) else None
    return None


# ---------------------------------------------------------------------------
# Careers-page fallback
#
# Name-guessing only finds boards whose slug derives from the company name.
# This fallback handles the rest: find the company's own site, read its careers
# page, and lift the real board token out of the ATS links embedded in it.
#
# The token is always read from the page, never guessed. The *domain* is
# guessed, so a homepage is only trusted once its own content names the
# company — the same impostor defense used for first-word slugs.
#
# Opt-in via --careers-fallback. It is off by default so the monthly
# resolve_ats.yml run keeps hitting only ATS JSON APIs and never crawls
# third-party websites as a side effect.
# ---------------------------------------------------------------------------

CAREERS_UA = USER_AGENT
PAGE_TIMEOUT = 8.0   # careers pages only; a homepage slower than this is rarely worth the wait
WEBSITE_DELAY_SECONDS = 1.0  # far more courteous than the ATS API delay
MAX_CAREERS_PAGES = 5        # careers links followed per company
MAX_PAGE_BYTES = 2_000_000

DEFAULT_CACHE_DIR = REPO_ROOT / ".ats_cache"

# Path segments that are structural, not board tokens.
RESERVED_TOKENS = {
    "embed", "job_board", "job-board", "api", "v1", "v0", "jobs", "job",
    "postings", "boards", "board", "careers", "career", "static", "assets",
    "images", "img", "css", "js", "search", "list", "index", "en-us", "en",
    # vendor-site subdomains that are the marketing site, not a customer board
    "www", "apply", "recruiting", "support", "help", "blog", "app", "info",
    "docs", "about", "partners", "developer", "status", "resources",
}

# %XX is allowed: Ashby tokens are display names, so spaces arrive URL-encoded
# ("Redesign%20Health"). Captured tokens are unquoted before use.
_TOK = r"[A-Za-z0-9][A-Za-z0-9_.%-]*"

# Ordered: the most specific/authoritative URL shapes first, so an API URL in
# the page beats a marketing link that happens to match a looser pattern.
ATS_PAGE_MARKERS: list[tuple[str, str, "re.Pattern[str]"]] = [
    ("greenhouse", "greenhouse.io", re.compile(r"boards\.greenhouse\.io/embed/job_board/js\?for=(" + _TOK + ")")),
    ("greenhouse", "greenhouse.io", re.compile(r"boards\.greenhouse\.io/embed/job_board\?for=(" + _TOK + ")")),
    # Greenhouse's documented embed div carries the token as a data attribute.
    ("greenhouse", "data-board-token", re.compile(r"data-board-token\s*=\s*[\"'](" + _TOK + r")[\"']")),
    ("greenhouse", "greenhouse.io", re.compile(r"(?:boards-api|api)\.greenhouse\.io/v1/boards/(" + _TOK + ")")),
    ("greenhouse", "greenhouse.io", re.compile(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/)?(" + _TOK + ")")),
    # grnhse.io is Greenhouse's short domain; it shares no substring with
    # "greenhouse.io", so none of the patterns above can ever see it.
    ("greenhouse", "grnhse.io", re.compile(r"grnhse\.io/(?:embed/job_board\?for=)?(" + _TOK + ")")),
    ("lever", "lever.co", re.compile(r"api\.lever\.co/v0/postings/(" + _TOK + ")")),
    ("lever", "lever.co", re.compile(r"jobs\.(?:eu\.)?lever\.co/(" + _TOK + ")")),
    ("ashby", "ashbyhq.com", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/(" + _TOK + ")")),
    ("ashby", "ashbyhq.com", re.compile(r"jobs\.ashbyhq\.com/(" + _TOK + ")")),
    ("smartrecruiters", "smartrecruiters.com", re.compile(r"api\.smartrecruiters\.com/v1/companies/(" + _TOK + ")")),
    ("smartrecruiters", "smartrecruiters.com", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/(" + _TOK + ")")),
    ("rippling", "rippling.com", re.compile(r"ats\.rippling\.com/(" + _TOK + ")")),
    # Rippling's newer board host puts the slug in the subdomain instead.
    ("rippling", "rippling-ats.com", re.compile(r"(" + _TOK + r")\.rippling-ats\.com")),
    # Workable job links are /j/<code>; only the account path is a board token.
    ("workable", "workable.com", re.compile(r"apply\.workable\.com/(?!j/)(" + _TOK + ")")),
    ("workable", "workable.com", re.compile(r"apply\.workable\.com/api/v\d/widget/accounts/(" + _TOK + ")")),
    # The embed.js widget carries no board URL at all -- the account is the
    # numeric argument to whr_embed(). That number is the same account key the
    # widget API takes, and the returned jobs carry the company's own apply
    # URLs, so the content check can still confirm it.
    ("workable", "whr_embed", re.compile(r"whr_embed\(\s*(\d{3,})")),
    ("recruitee", "recruitee.com", re.compile(r"(" + _TOK + r")\.recruitee\.com")),
    ("bamboohr", "bamboohr.com", re.compile(r"(" + _TOK + r")\.bamboohr\.com")),
    ("breezy", "breezy.hr", re.compile(r"(" + _TOK + r")\.breezy\.hr")),
    ("teamtailor", "teamtailor.com", re.compile(r"(" + _TOK + r")\.teamtailor\.com")),
    ("personio", "personio.", re.compile(r"(" + _TOK + r")\.jobs\.personio\.[a-z]{2,3}")),
    # --- SRC-13, Sep 2026: shapes read off cached careers pages ---
    # ADP Workforce Now: a recruitment.html link or the <recruitment-current-
    # openings cid="..."> widget. Only the cid identifies the board.
    ("adp", "workforcenow.adp.com",
     re.compile(r"recruitment\.html\?(?:[^\"'\s<>]*?(?:&|&amp;|&#038;))?cid=([0-9a-fA-F-]{36})")),
    ("adp", "recruitment-current-openings",
     re.compile(r"<recruitment-current-openings[^>]*?\bcid\s*=\s*[\"']([0-9a-fA-F-]{36})")),
    ("gem", "jobs.gem.com", re.compile(r"jobs\.gem\.com/(" + _TOK + ")")),
    ("jobscore", "jobscore.com", re.compile(r"careers\.jobscore\.com/(?:careers|jobs)/(" + _TOK + ")")),
    ("manatal", "careers-page.com", re.compile(r"(" + _TOK + r")\.careers-page\.com")),
    ("manatal", "careers-page.com", re.compile(r"careers-page\.com/(" + _TOK + ")")),
]

# UKG Ready (saashr.com): host + numeric company id, captured as "host:id".
UKG_READY_MARKER = re.compile(r"(secure\d*\.saashr\.com)/ta/(\d{4,})\.careers", re.I)
_UKG_READY_LITERAL = "saashr.com"

# Comeet prints its API credentials into the page in one of three shapes: the
# WordPress plugin's settings JSON, the careers-api embed's data attributes, or
# the COMPANY_DATA blob on its own hosted page (www.comeet.com/jobs/<slug>/<uid>).
_COMEET_LITERAL = "comeet"
COMEET_CREDENTIAL_MARKERS = [
    re.compile(r"comeet_token[\"']?\s*[:=]\s*[\"']([0-9A-Fa-f]{20,})[\"'].{0,200}?"
               r"comeet_uid[\"']?\s*[:=]\s*[\"']([0-9A-Fa-f]{1,4}\.[0-9A-Fa-f]{3})[\"']", re.S),
    re.compile(r"data-company-uid\s*=\s*[\"']([0-9A-Fa-f]{1,4}\.[0-9A-Fa-f]{3})[\"'][^>]*?"
               r"data-token\s*=\s*[\"']([0-9A-Fa-f]{20,})[\"']", re.S),
    re.compile(r"[\"']company_uid[\"']\s*:\s*[\"']([0-9A-Fa-f]{1,4}\.[0-9A-Fa-f]{3})[\"']\s*,\s*"
               r"[\"']token[\"']\s*:\s*[\"']([0-9A-Fa-f]{20,})[\"']"),
]
# A link to Comeet's hosted board carries the uid but not the token; the hosted
# page itself carries both, so it is worth one extra fetch.
COMEET_HOSTED_LINK = re.compile(
    r"https?://www\.comeet\.com/jobs/(" + _TOK + r")/([0-9A-Fa-f]{1,4}\.[0-9A-Fa-f]{3})")

# The asset path only a Teamtailor-hosted career site loads. A plain jobs.js
# widget on someone's page does not match -- that page is not the career site.
_TEAMTAILOR_SITE_LITERAL = "teamtailor-cdn.com/assets/careersite"

# Oracle Cloud HCM carries host + site code, like Workday's tenant + site.
ORACLE_HCM_MARKER = re.compile(
    r"(" + _TOK + r"\.fa\.[a-z0-9]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"(?:[a-z]{2}(?:-[A-Z]{2})?/)?sites/(" + _TOK + ")"
)

# Workday carries two identifiers (tenant + site); captured as "tenant:site".
WORKDAY_MARKER = re.compile(
    r"(" + _TOK + r")\.(wd\d+)\.myworkdayjobs\.com/(?:(?:[a-z]{2}-[A-Z]{2})/)?(" + _TOK + ")"
)

_CAREERS_HINT = re.compile(r"career|jobs|join-us|join_us|joinus|hiring|work-with-us|opportunities", re.I)
# Link *text* worth following even when the href gives nothing away
# ("Careers" -> /company/life-here). Anchored on words, not substrings, so
# "jobs" in "steve-jobs-tribute" does not qualify on its own.
_CAREERS_TEXT_HINT = re.compile(
    r"\b(careers?|jobs?|hiring|vacanc(?:y|ies)|openings?|opportunities|"
    r"join\s+(?:us|our|the)|work\s+(?:with|for|at)\s+us|life\s+at|"
    r"open\s+(?:roles|positions)|we[''\u2019]?re\s+hiring)\b", re.I)
# <a ...href="X"...>TEXT</a>, lazily bounded by the closing tag.
_ANCHOR_RE = re.compile(
    r"""<a\b([^>]*?)href\s*=\s*["']([^"'>\s]+)["']([^>]*)>(.{0,300}?)</a>""",
    re.I | re.S)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")

# Careers-ish subdomains, tried when the landing page yields nothing.
CAREERS_SUBDOMAINS = ("jobs", "careers", "boards", "apply")

# Vendors we can see but cannot resolve to a JSON board. Detected only to
# explain a failure ("unsupported ATS: icims"), never treated as a hit.
# Checked only after detect_ats_in_page found nothing usable, so a vendor that
# is also in ATS_PAGE_MARKERS (adp, comeet, gem, jobscore, manatal) lands here
# only when the page named the vendor but carried no readable board token.
UNSUPPORTED_ATS_MARKERS: list[tuple[str, str]] = [
    ("icims", "icims.com"),
    ("taleo", "taleo.net"),
    ("jobvite", "jobvite.com"),
    ("jazzhr", "applytojob.com"),
    ("ultipro", "ultipro.com"),
    ("adp", "workforcenow.adp.com"),
    ("adp", "myjobs.adp.com"),
    ("adp", "recruiting.adp.com"),
    ("paylocity", "recruiting.paylocity.com"),
    ("dayforce", "dayforcehcm.com"),
    ("successfactors", "successfactors.com"),
    ("eightfold", "eightfold.ai"),
    ("phenom", "phenompeople.com"),
    ("avature", "avature.net"),
    ("pinpoint", "pinpointhq.com"),
    ("getro", "getro.com"),
    ("paycom", "paycomonline.net"),
    ("paycor", "paycor.com"),
    ("clearcompany", "clearcompany.com"),
    ("hirebridge", "hirebridge.com"),
    ("silkroad", "silkroad.com"),
    ("workbright", "workbright.com"),
    ("bullhorn", "bullhornstaffing.com"),
    ("gusto", "gusto.com/careers-page"),
    ("notion", "notion.site"),
    ("airtable", "airtable.com/shr"),
    ("isolvedhire", "isolvedhire.com"),
    ("applicantpro", "applicantpro.com"),
    ("applicantstack", "applicantstack.com"),
    ("recruiterbox", "recruiterbox.com"),
    ("workatastartup", "workatastartup.com"),
    ("hirehive", "hirehive.com"),
    ("smartsheet_form", "app.smartsheet.com/b/form"),
    ("google_form", "docs.google.com/forms"),
    ("job_board_only:linkedin", "linkedin.com/jobs"),
    ("job_board_only:indeed", "indeed.com/cmp"),
    ("job_board_only:ziprecruiter", "ziprecruiter.com/jobs"),
    # --- added Aug 2026 from the bespoke_page post-mortem: every one of these
    # was seen on a real careers page that the resolver had filed as "bespoke".
    # None exposes a public JSON board we can read, so they are named, not
    # resolved -- "unsupported_ats:comeet" is a far more actionable dead end
    # than "bespoke_page". Literals are the narrowest form that still matches
    # what was observed; a bare vendor domain would catch marketing links.
    ("comeet", "comeet.co"),          # also matches comeet.com
    ("zohorecruit", "zohorecruit."),  # .com / .eu / .in
    ("ukg_ready", "saashr.com"),
    ("jobscore", "jobscore.com"),
    ("hrsmart", "hrsmart.com"),
    ("hrmdirect", "hrmdirect.com"),
    ("careerplug", "careerplug.com"),
    ("catsone", "catsone.com"),
    ("polymer", "jobs.polymer.co"),
    ("gem", "jobs.gem.com"),
    ("kula", "careers.kula.ai"),
    ("happydance", "happydance.website"),
    # careers-page.com is Manatal's hosted careers domain, not a Recruitee
    # white-label as first assumed (exequt.recruitee.com 404s, Manatal's API
    # serves careers-page.com/exequt). A page with a readable slug resolves via
    # ATS_PAGE_MARKERS; this names the vendor when no slug could be read.
    ("manatal", "careers-page.com"),
    # A Teamtailor-hosted careers site on the company's own domain: the vendor
    # is unmistakable but the board slug never appears, only CDN asset URLs.
    # A page carrying a real <slug>.teamtailor.com link resolves instead --
    # detect_ats_in_page runs first and wins.
    ("teamtailor", "teamtailor-cdn.com"),
    # JazzHR's white-label careers page carries only a "powered by" footer;
    # the applytojob.com marker above catches the hosted form.
    ("jazzhr", "jazzhr.com"),
]

# Most informative diagnosis wins: a careers subpage naming iCIMS is a better
# answer than the landing page looking bespoke, whichever we saw first.
_REASON_RANK = {
    "no_site": 0, "no_careers_page": 1, "bespoke_page": 2,
    "unsupported_ats": 3, "stale_board": 4, "content_check_failed": 5,
    # Highest, because it is the only one that is not a verdict: the page has a
    # board we support and we could not read it. That is the row to retry.
    "board_unavailable": 6,
}


def note_reason(report: dict[str, Any], reason: str, url: str = "") -> None:
    """Record a failure reason, keeping the most specific one seen.

    `url` is the page the reason was read off, kept in step with the reason it
    belongs to. Without it an unresolved row cannot tell "no careers page
    anywhere" from "this careers page carries no board we can read", and the
    page has to be found again from scratch to know which it was.
    """
    current = report.get("reason", "")
    rank = lambda r: _REASON_RANK.get(r.split(":")[0], -1)  # noqa: E731
    if rank(reason) >= rank(current):
        report["reason"] = reason
        report["careers_url"] = url


def detect_unsupported_ats(html: str) -> str | None:
    """Name a hiring vendor present on the page that we cannot resolve."""
    haystack = html.lower()
    for vendor, literal in UNSUPPORTED_ATS_MARKERS:
        if literal in haystack:
            return vendor
    return None
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'>\s]+)["']""", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


# Every marker pattern carries the literal its match requires. The _TOK prefix
# patterns backtrack hard across long alphanumeric runs (minified JS, base64
# data URIs), costing seconds on a large page — and `re` holds the GIL, so that
# blocks the whole event loop. A literal substring test is linear and skips the
# regex entirely for the overwhelming majority of pages, which contain no board
# link at all. The guard is strictly more permissive than the patterns (which
# are case-sensitive), so it can never hide a real match.
_ORACLE_HCM_LITERAL = "oraclecloud.com"
_WORKDAY_LITERAL = "myworkdayjobs.com"


# Patterns whose token is a *prefix* of the vendor domain ("acme.recruitee.com")
# backtrack quadratically when finditer runs over a whole page: every start
# position in a long alphanumeric run (minified JS, base64) re-scans that run.
# statsig.com/careers (419 KB) took 38 s in the Personio pattern alone, and the
# event loop is blocked for all of it -- this pinned the SRC-12 crawl at 100%
# CPU. These patterns are run only on a window around each occurrence of their
# anchor. A DNS label is at most 63 characters, so the window loses no real
# token; the tail leaves room for Workday's site segment after the host.
_PREFIX_WINDOW = 256
_PREFIX_ANCHORS = {
    "rippling-ats.com": ".rippling-ats.com",
    "recruitee.com": ".recruitee.com",
    "bamboohr.com": ".bamboohr.com",
    "breezy.hr": ".breezy.hr",
    "teamtailor.com": ".teamtailor.com",
    "personio.": ".jobs.personio.",
    "careers-page.com": ".careers-page.com",
}


def _windowed_finditer(pattern: "re.Pattern[str]", html: str, haystack: str, anchor: str):
    """pattern.finditer(html), evaluated only in windows around each anchor hit."""
    seen_spans: set[tuple[int, int]] = set()
    start = haystack.find(anchor)
    while start != -1:
        lo = max(0, start - _PREFIX_WINDOW)
        hi = start + len(anchor) + _PREFIX_WINDOW
        for match in pattern.finditer(html, lo, hi):
            # A match must use this anchor occurrence, not end short of it.
            if match.end() < start + len(anchor):
                continue
            span = (match.start(), match.end())
            if span not in seen_spans:
                seen_spans.add(span)
                yield match
        start = haystack.find(anchor, start + 1)


def detect_ats_in_page(html: str, page_url: str = "") -> list[tuple[str, str, str]]:
    """Every (ats, token, evidence) ATS marker on the page, best candidate first.

    Ordered by ATS_PAGE_MARKERS, so a definitive API URL outranks a looser
    marketing link. Structural path segments are rejected as tokens, and tokens
    are URL-decoded ("Redesign%20Health" -> "Redesign Health").

    A list rather than one hit: careers pages carry stale board links, so the
    caller validates candidates in order and takes the first that is live.

    `page_url` is needed only for a Teamtailor career site on the company's own
    domain, where the board token is the page's host rather than anything in
    the markup.
    """
    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    haystack = html.lower()

    def add(ats: str, token: str, evidence: str) -> None:
        if (ats, token) not in seen:
            seen.add((ats, token))
            found.append((ats, token, evidence))

    for ats, literal, pattern in ATS_PAGE_MARKERS:
        if literal not in haystack:
            continue
        anchor = (_PREFIX_ANCHORS.get(literal)
                  if pattern.pattern.startswith("(" + _TOK + ")") else None)
        matches = (_windowed_finditer(pattern, html, haystack, anchor) if anchor
                   else pattern.finditer(html))
        for match in matches:
            token = unquote(match.group(1)).strip()
            if token.lower() in RESERVED_TOKENS or len(token) < 2:
                continue
            if (ats, token) not in seen:
                seen.add((ats, token))
                found.append((ats, token, match.group(0)))
    for match in (_windowed_finditer(ORACLE_HCM_MARKER, html, haystack, ".oraclecloud.com/hcmui")
                  if _ORACLE_HCM_LITERAL in haystack else ()):
        host, site = unquote(match.group(1)), unquote(match.group(2))
        token = f"{host}:{site}"
        if ("oracle_hcm", token) not in seen:
            seen.add(("oracle_hcm", token))
            found.append(("oracle_hcm", token, match.group(0)))
    for match in (_windowed_finditer(WORKDAY_MARKER, html, haystack, ".myworkdayjobs.com")
                  if _WORKDAY_LITERAL in haystack else ()):
        tenant, _datacenter, site = match.group(1), match.group(2), match.group(3)
        if tenant.lower() in RESERVED_TOKENS:
            continue
        token = f"{unquote(tenant)}:{unquote(site)}"
        if ("workday", token) not in seen:
            seen.add(("workday", token))
            found.append(("workday", token, match.group(0)))
    for match in (UKG_READY_MARKER.finditer(html)
                  if _UKG_READY_LITERAL in haystack else ()):
        add("ukg_ready", f"{match.group(1).lower()}:{match.group(2)}", match.group(0))
    if _COMEET_LITERAL in haystack or "company_uid" in haystack:
        for pattern in COMEET_CREDENTIAL_MARKERS:
            for match in pattern.finditer(html):
                a, b = match.group(1), match.group(2)
                uid, token = (a, b) if "." in a else (b, a)
                add("comeet", f"{uid.upper()}:{token}", match.group(0)[:200])
    if page_url and _TEAMTAILOR_SITE_LITERAL in haystack:
        host = urlsplit(page_url).netloc.lower()
        # A *.teamtailor.com host is already caught by the subdomain pattern.
        if host and not host.endswith("teamtailor.com"):
            add("teamtailor", host, _TEAMTAILOR_SITE_LITERAL)
    return found


def comeet_hosted_pages(html: str) -> list[str]:
    """Comeet hosted-board URLs on a page that carries no Comeet credentials.

    The hosted page prints the uid and API token, so one extra fetch turns a
    bare "powered by Comeet" link into a readable board.
    """
    if _COMEET_LITERAL not in html.lower():
        return []
    if any(p.search(html) for p in COMEET_CREDENTIAL_MARKERS):
        return []
    urls = {f"https://www.comeet.com/jobs/{m.group(1)}/{m.group(2).upper()}"
            for m in COMEET_HOSTED_LINK.finditer(html)}
    return sorted(urls)


AGGREGATOR_MIN_BOARDS = 3


def is_aggregator_page(found: list[tuple[str, str, str]]) -> bool:
    """True if a page links to AGGREGATOR_MIN_BOARDS or more distinct boards.

    One company's careers page carries one board, occasionally two (a stale
    link beside the current one, or a subsidiary). Three or more is a list of
    other employers' boards.
    """
    boards = {(ats, token.split(":")[0].lower()) for ats, token, _ in found}
    return len(boards) >= AGGREGATOR_MIN_BOARDS


# Lever runs a separate EU instance; a jobs.eu.lever.co link needs the EU API.
LEVER_EU_TEMPLATE = "https://api.eu.lever.co/v0/postings/{slug}?mode=json"


def board_api_url(ats: str, token: str, evidence: str = "") -> str:
    """Public JSON API URL for a board token, or "" when there is no uniform one."""
    if ats == "workday":
        return ""  # tenant+site+datacenter; no single public endpoint shape
    if ats == "oracle_hcm":
        host, _, site = token.partition(":")
        return ORACLE_HCM_TEMPLATE.format(host=host, site=site or "CX_1")
    if ats == "ukg_ready":
        host, _, company_id = token.partition(":")
        return UKG_READY_TEMPLATE.format(host=host, company_id=company_id)
    if ats == "comeet":
        uid, _, api_token = token.partition(":")
        return COMEET_TEMPLATE.format(uid=uid, token=api_token)
    if ats == "teamtailor" and "." in token:
        return TEAMTAILOR_SITE_TEMPLATE.format(host=token)
    if ats == "lever" and "eu.lever.co" in evidence:
        template = LEVER_EU_TEMPLATE
    elif ats in PAGE_ONLY_PROBES:
        template = PAGE_ONLY_PROBES[ats][0]
    else:
        template = ATS_PROBES[ats][0]
    return template.format(slug=quote(token, safe=""))


def board_validator(ats: str):
    """Validator for a board payload, including page-only providers."""
    if ats == "oracle_hcm":
        return _oracle_hcm_has_jobs
    if ats in PAGE_ONLY_PROBES:
        return PAGE_ONLY_PROBES[ats][1]
    return ATS_PROBES[ats][1]


class BoardUnavailable(Exception):
    """The board API refused to answer — throttled or blocked, not absent."""

    def __init__(self, ats: str, url: str, status: int) -> None:
        super().__init__(f"{ats} board API returned {status}: {url}")
        self.ats, self.url, self.status = ats, url, status


async def validate_board(
    client: httpx.AsyncClient,
    ats: str,
    token: str,
    evidence: str,
    throttle: HostThrottle,
    semaphore: asyncio.Semaphore,
) -> tuple[bool, bool, str, Any]:
    """Confirm a page-scraped token is a real board.

    Returns (exists, has_jobs, url, data) — the parsed board is handed back so
    the caller can run the same content check stage 1 applies, which is what
    keeps a third-party widget on someone's jobs page from resolving as theirs.

    Distinct from probe(): an existing board with zero current openings still
    counts as resolved — the token is correct and will carry jobs later. Only a
    non-200 means the link on the careers page was stale.

    429 and 403 are the exception, and they are raised rather than reported as
    absence. A vendor rate-limiting us says nothing about whether the board
    exists, and treating it as absence writes `stale_board` over a live board —
    silently, and for every company on that vendor at once.
    """
    url = board_api_url(ats, token, evidence)
    if not url:
        return True, False, evidence, None  # Workday: no JSON endpoint to check
    async with semaphore:
        await throttle.wait(urlsplit(url).netloc)
        try:
            response = await client.get(url)
        except (httpx.HTTPError, httpx.InvalidURL):
            return False, False, url, None
    if response.status_code in (403, 429):
        raise BoardUnavailable(ats, url, response.status_code)
    if response.status_code != 200:
        return False, False, url, None
    try:
        data = response.json()
    except ValueError:
        return False, False, url, None
    return True, bool(board_validator(ats)(data)), url, data


def domain_candidates(name: str) -> list[str]:
    """Plausible own-domain hosts for a company name, most likely first."""
    words = _name_words(name)
    if not words:
        return []
    trimmed = words
    while len(trimmed) > 1 and trimmed[-1] in LEGAL_SUFFIXES:
        trimmed = trimmed[:-1]
    stems: list[str] = []
    for parts in ([words, trimmed] if trimmed != words else [words]):
        stems.append("".join(parts))
        if len(parts) > 1:
            stems.append("-".join(parts))
    hosts: list[str] = []
    for stem in dict.fromkeys(stems):
        for tld in (".com", ".io", ".ai", ".co"):
            hosts.append(stem + tld)
    return list(dict.fromkeys(hosts))


def page_confirms_company(html: str, name: str) -> bool:
    """True if the page's own text names the company — proves the domain is theirs."""
    text = _TAG_RE.sub(" ", html)
    title = " ".join(_TITLE_RE.findall(text))
    body = re.sub(r"<[^>]+>", " ", text)
    return name_phrase(name) in _normalize_text(title + " " + body[:20000])


def careers_links(html: str, base_url: str) -> list[str]:
    """Careers-ish links on a page, in page order, href matches first.

    A link qualifies on its href ("/careers") or on its visible text
    ("Careers", "We're hiring") — the latter catches sites whose careers page
    lives at an unguessable path, and links straight out to an ATS domain.
    Off-site links are kept: a careers link pointing at another domain is
    exactly the rebrand/parent-company case worth following.
    """
    by_href: list[str] = []
    by_text: list[str] = []
    seen: set[str] = set()
    for pre, href, post, inner in _ANCHOR_RE.findall(html):
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        href_hit = bool(_CAREERS_HINT.search(href))
        if not href_hit:
            label = _STRIP_TAGS_RE.sub(" ", inner)
            # aria-label/title carry the wording when the anchor wraps an icon.
            label += " " + pre + " " + post
            if not _CAREERS_TEXT_HINT.search(label):
                continue
        url = urljoin(base_url, href)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        (by_href if href_hit else by_text).append(url)
    return by_href + by_text


class PageCache:
    """Disk cache of fetched pages, so re-runs cost no external requests."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def get(self, url: str) -> dict[str, Any] | None:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None

    def put(self, url: str, status: int, text: str, final_url: str | None = None) -> None:
        self._path(url).write_text(json.dumps(
            {"url": url, "status": status, "text": text, "final_url": final_url or url}))


class RobotsCache:
    """Per-host robots.txt, fetched once. Unreachable robots.txt means allow."""

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    async def allows(self, client: httpx.AsyncClient, url: str, throttle: HostThrottle) -> bool:
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._parsers:
            parser = urllib.robotparser.RobotFileParser()
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                await throttle.wait(host)
                response = await client.get(robots_url, timeout=PAGE_TIMEOUT)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser.parse([])  # no robots.txt => everything allowed
            except Exception:
                parser.parse([])
            self._parsers[host] = parser
        try:
            return self._parsers[host].can_fetch(CAREERS_UA, url)
        except Exception:
            return True


class Fetched(NamedTuple):
    """A fetched page plus the URL it actually came from after redirects."""

    text: str
    url: str


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    throttle: HostThrottle,
    semaphore: asyncio.Semaphore,
    cache: PageCache,
    robots: RobotsCache,
) -> Fetched | None:
    """GET a page, honoring robots.txt, the disk cache, and rate limits.

    Returns the body together with the final URL, which differs from the
    requested one when a company has rebranded and its old careers page
    redirects to the new domain.

    Read-only: GET requests only, no cookies persisted, no form submission.
    """
    cached = cache.get(url)
    if cached is not None:
        text = cached["text"] or ""
        # Entries written before final_url was tracked fall back to the request
        # URL, which is correct for everything that was not redirected.
        return Fetched(text, cached.get("final_url") or url) if text else None

    if not await robots.allows(client, url, throttle):
        log.debug("robots.txt disallows %s", url)
        cache.put(url, 999, "")
        return None

    async with semaphore:
        await throttle.wait(urlsplit(url).netloc)
        try:
            response = await client.get(url, timeout=PAGE_TIMEOUT)
        except Exception as exc:
            log.debug("careers fetch failed %s: %s", url, exc)
            return None

    final_url = str(response.url)
    ctype = response.headers.get("content-type", "")
    if response.status_code != 200 or "html" not in ctype.lower():
        cache.put(url, response.status_code, "", final_url)
        return None
    text = response.text[:MAX_PAGE_BYTES]
    cache.put(url, 200, text, final_url)
    return Fetched(text, final_url)


# Dropped when forming an initialism: nobody puts them in an acronym.
_INITIALISM_STOPWORDS = {"for", "of", "and", "the", "a", "an", "in", "on", "at", "to"}


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def board_belongs_to(
    name: str,
    ats: str,
    token: str,
    data: Any,
    careers_url: str,
    origin_host: str,
) -> bool:
    """True if a page-scraped board really belongs to this company.

    Stage 1 verifies a guessed slug by checking the board's contents name the
    company. Careers-page hits need the same check, because a page that embeds
    someone else's hiring widget otherwise resolves to that third party --
    observed: a trade association's jobs page resolving to a toy manufacturer.

    Three ways to pass, in order of strength:
      1. the board's own contents name the company (the stage-1 check);
      2. the token matches the company's own site after redirects --
         this is the rebrand/acquisition case, where the old domain redirects
         to the new company and the board is correctly under the new name;
      3. the token matches the company name directly.
    Workday and Oracle expose no JSON board to read, so they rely on 2 and 3.
    """
    if data is not None and board_matches_company(name, ats, data):
        return True

    # ADP and UKG Ready tokens are opaque ids and their postings rarely print
    # the employer's name, so neither check below can ever pass for them. What
    # does identify the board is where it was embedded: a widget on a page of
    # the company's own confirmed site. A page on any other domain proves
    # nothing -- that is the off-site-link case described below. Callers pass
    # data=None for aggregator pages, which keeps this rule off those too.
    if ats in OPAQUE_TOKEN_ATS:
        if data is None:
            return False
        page_host = urlsplit(careers_url).netloc if careers_url else ""
        return bool(page_host and origin_host
                    and throttle_key(page_host) == throttle_key(origin_host))

    # Workday/Oracle tokens are "tenant:site"; the tenant carries the identity.
    tenant = _alnum((token or "").split(":")[0])
    if not tenant:
        return False

    # Only the origin host counts, and only because it is taken *after*
    # redirects: an old domain that 302s to a rebrand really is that company.
    # The host of the page the link sat on must NOT be trusted -- following an
    # off-site careers link to another company would otherwise rubber-stamp it
    # (observed: "Quantum Computing" linking out to D-Wave's board).
    stem = _alnum(throttle_key(origin_host).rsplit(".", 1)[0]) if origin_host else ""
    if stem and (stem == tenant or stem in tenant or tenant in stem):
        return True

    company = _alnum(name)
    if company and (company == tenant or company in tenant or tenant in company):
        return True
    # Every substantial word must appear, and together they must account for
    # most of the token. Matching on any single word let a common one carry an
    # unrelated board -- "Quantum Computing" accepting "dwavequantum" because
    # "quantum" happened to be a substring.
    words = [_alnum(w) for w in _name_words(name) if len(w) >= 5]
    if words and all(w in tenant for w in words):
        if len("".join(words)) / max(len(tenant), 1) >= 0.5:
            return True

    # Organizations that brand themselves by their initials never spell the
    # full name out on the board, so the content check cannot see it -- e.g.
    # "Coalition for Green Capital" hiring under the token "cgc". Exact match
    # only, and at least three letters, so short tokens cannot collide.
    significant = [w for w in _name_words(name) if w not in _INITIALISM_STOPWORDS]
    initials = "".join(w[0] for w in significant if w)
    return len(initials) >= 3 and initials == tenant


async def resolve_via_careers(
    client: httpx.AsyncClient,
    name: str,
    throttle: HostThrottle,
    semaphore: asyncio.Semaphore,
    cache: PageCache,
    robots: RobotsCache,
    seed_url: str | None = None,
    api_throttle: HostThrottle | None = None,
    report: dict[str, Any] | None = None,
    seed_only: bool = False,
) -> dict[str, Any] | None:
    """Find a company's board by reading its own careers page.

    Order of attack per origin: read the landing page, follow its real careers
    links (by href or by link text, on- or off-site), then fall back to the
    common careers subdomains. Every board token is read from the page, never
    guessed, validated against the board API, and then content-checked.

    seed_url, when given, is a known careers/home URL and is tried before any
    guessed domain. It is content-checked exactly like a guessed domain: a
    supplied website can be stale, parked, or point at an acquirer, and taking
    it on trust would let another company's board through the one gate that
    catches that.

    `report`, when passed, is filled with a `reason` explaining a failure, so a
    run can distinguish "no careers page" from "an ATS we do not support", plus
    a `careers_url` naming the page that reason came from.

    seed_only (SRC-17): read the seed site and what it links to, never a
    guessed domain. A guessed domain that happens to name the company is how
    Umbra (umbra.space) ended up recorded on umbraco.com.
    """
    report = report if report is not None else {}
    note_reason(report, "no_site")
    api_throttle = api_throttle or throttle

    async def examine(page: Fetched, origin_host: str, follow: bool = True) -> dict[str, Any] | None:
        """Check one page for a board; record why it did not yield one."""
        found = detect_ats_in_page(page.text, page.url)
        # A page linking to several different boards is an aggregator -- a VC
        # portfolio jobs page (jobs.menlovc.com), an accelerator, a directory.
        # Portfolio postings routinely say "backed by <the VC>", so the content
        # check passes on every one of them and the first board on the page
        # would resolve under the VC's name. On such a page only the identity
        # checks (token matches the company or its own domain) may pass.
        aggregator = is_aggregator_page(found)
        for ats, token, evidence in found:
            try:
                exists, has_jobs, api_url, data = await validate_board(
                    client, ats, token, evidence, api_throttle, semaphore
                )
            except BoardUnavailable as exc:
                log.warning("board API unavailable for %s: %s", name, exc)
                note_reason(report, f"board_unavailable:{ats}", page.url)
                continue
            if not exists:
                log.info("stale board link for %s on %s: %s / %s", name, page.url, ats, token)
                note_reason(report, "stale_board", page.url)
                continue
            if not board_belongs_to(name, ats, token, None if aggregator else data,
                                    page.url, origin_host):
                log.info("rejected %s / %s for %s — board is not theirs (%s)",
                         ats, token, name, page.url)
                note_reason(report, "content_check_failed", page.url)
                report["rejected"] = f"{ats}/{token}"
                continue
            log.info("resolved %-35s -> %s / %s  (careers page: %s)", name, ats, token, page.url)
            return {
                "name": name, "ats": ats, "slug": token,
                "resolved_at": datetime.date.today().isoformat(),
                "board_url": api_url,
                "source": "careers_page",
                "careers_url": page.url,
                "has_open_jobs": has_jobs,
            }
        # A bare link to Comeet's hosted board: that page carries the API
        # credentials this one lacks. Fetched once, never followed further.
        for hosted_url in (comeet_hosted_pages(page.text)[:2] if follow and not aggregator else []):
            hosted = await fetch_page(client, hosted_url, throttle, semaphore, cache, robots)
            if hosted:
                hit = await examine(hosted, origin_host, follow=False)
                if hit:
                    return hit
        vendor = detect_unsupported_ats(page.text)
        note_reason(report, f"unsupported_ats:{vendor}" if vendor else "bespoke_page",
                    page.url)
        return None

    origins: list[tuple[str, bool]] = []
    if seed_url:
        origins.append((seed_url, True))
    if not (seed_only and seed_url):
        origins += [(f"https://{host}", True) for host in domain_candidates(name)]

    for origin, needs_confirmation in origins:
        landing = await fetch_page(client, origin, throttle, semaphore, cache, robots)
        if not landing:
            continue
        if needs_confirmation and not page_confirms_company(landing.text, name):
            log.debug("domain %s does not name %s — not their site", origin, name)
            continue
        # The landing page's *final* host is the company's real home: an old
        # domain that redirects to a rebrand hands us the new one here.
        origin_host = urlsplit(landing.url).netloc or urlsplit(origin).netloc
        note_reason(report, "no_careers_page")

        hit = await examine(landing, origin_host)
        if hit:
            return hit

        candidates = careers_links(landing.text, landing.url)[:MAX_CAREERS_PAGES]
        candidates += [f"https://{sub}.{throttle_key(origin_host)}"
                       for sub in CAREERS_SUBDOMAINS]
        seen_pages = {landing.url}
        for link in candidates:
            page = await fetch_page(client, link, throttle, semaphore, cache, robots)
            if not page or page.url in seen_pages:
                continue
            seen_pages.add(page.url)
            hit = await examine(page, origin_host)
            if hit:
                return hit
    return None


async def resolve_company(
    client: httpx.AsyncClient,
    name: str,
    throttle: HostThrottle,
    semaphore: asyncio.Semaphore,
    ats_names: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Try every slug candidate against all four ATS APIs; return the first hit.
    `ats_names`, when given, limits the probes to those vendors (main.py
    resolve probes Workable separately, on its own paced queue).

    Candidates are tried in order; for each candidate the four ATS probes run
    concurrently, and on a multi-hit the ATS_PROBES declaration order decides.
    First-word-only candidates must additionally pass the name-phrase check
    (board_matches_company) — a hit that fails it is an unrelated company that
    happens to own the slug. Returns None if nothing matches.
    """
    probes = {ats: p for ats, p in ATS_PROBES.items() if ats_names is None or ats in ats_names}
    for candidate in slug_candidates(name):
        urls = {
            ats: template.format(slug=candidate.slug)
            for ats, (template, _) in probes.items()
        }
        results = await asyncio.gather(
            *(
                probe(client, urls[ats], validator, throttle, semaphore)
                for ats, (_, validator) in probes.items()
            )
        )
        for (ats, _), data in zip(probes.items(), results):
            if data is None:
                continue
            if not slug_hit_accepted(name, ats, candidate, data):
                log.info(
                    "rejected %s / %s for %s — one-word name on a shared namespace, "
                    "or board content does not mention %r",
                    ats, candidate.slug, name, name_phrase(name),
                )
                continue
            log.info("resolved %-30s -> %s / %s", name, ats, candidate.slug)
            return {
                "name": name,
                "ats": ats,
                "slug": candidate.slug,
                "resolved_at": datetime.date.today().isoformat(),
                "board_url": urls[ats],
            }
    log.info("unresolved %s (tried %d slug candidates)", name, len(slug_candidates(name)))
    return None


def load_existing(path: Path) -> dict[str, Any]:
    """Load an existing companies.yaml, returning an empty structure if absent."""
    if not path.exists():
        return {"companies": [], "unresolved": [], "pinned": None}
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    return {
        "companies": data.get("companies") or [],
        "unresolved": data.get("unresolved") or [],
        # Hand-maintained (sources/watchlist.py reads it); carried through untouched.
        "pinned": data.get("pinned"),
    }


def read_company_names(path: Path) -> list[str]:
    """Read one company name per line, skipping blanks."""
    names = [line.strip() for line in path.read_text().splitlines()]
    return [n for n in names if n]


async def resolve_all(
    names: list[str],
    already_resolved: dict[str, dict[str, Any]],
    client: httpx.AsyncClient,
    careers_fallback: bool = False,
    cache_dir: Path | None = None,
    seed_urls: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve every name not already resolved; return (companies, unresolved).

    Both lists come back in the input file's order, with previously resolved
    entries carried over unchanged. When careers_fallback is on, companies that
    survive slug-guessing get a second pass that reads their own careers page,
    starting from seed_urls[name] (a known website) when one is supplied.
    """
    seed_urls = seed_urls or {}
    throttle = HostThrottle()
    semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)
    to_probe = [n for n in names if n not in already_resolved]

    results = await asyncio.gather(
        *(resolve_company(client, n, throttle, semaphore) for n in to_probe)
    )
    resolved_now = {n: r for n, r in zip(to_probe, results) if r is not None}

    if careers_fallback:
        leftovers = [n for n in to_probe if n not in resolved_now]
        log.info("careers-page fallback for %d unresolved companies", len(leftovers))
        web_throttle = HostThrottle(base_delay=WEBSITE_DELAY_SECONDS)
        cache = PageCache(cache_dir or DEFAULT_CACHE_DIR)
        robots = RobotsCache()
        careers_results = await asyncio.gather(
            *(
                resolve_via_careers(client, n, web_throttle, semaphore, cache, robots,
                                    seed_url=seed_urls.get(n.lower()))
                for n in leftovers
            )
        )
        for name, result in zip(leftovers, careers_results):
            if result is not None:
                resolved_now[name] = result

    companies: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for name in names:
        if name in already_resolved:
            companies.append(already_resolved[name])
        elif name in resolved_now:
            companies.append(resolved_now[name])
        else:
            unresolved.append({"name": name, "tried": list(ATS_PROBES)})
    return companies, unresolved


def write_yaml(path: Path, companies: list[dict[str, Any]], unresolved: list[dict[str, Any]],
               pinned: dict[str, Any] | None = None) -> None:
    """Write companies.yaml atomically-ish (single write, parent dir ensured).
    `pinned` is the file's hand-maintained pinned: section, written back as
    it was read; leaving it out would drop it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"companies": companies, "unresolved": unresolved}
    if pinned is not None:
        payload["pinned"] = pinned
    with path.open("w") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)


def print_summary(companies: list[dict[str, Any]], unresolved: list[dict[str, Any]]) -> None:
    """Print resolved counts by ATS and the explicit list of unresolved names."""
    counts: dict[str, int] = {}
    for entry in companies:
        counts[entry["ats"]] = counts.get(entry["ats"], 0) + 1
    print("\n=== Resolution summary ===")
    print(f"Resolved: {len(companies)} / {len(companies) + len(unresolved)}")
    for ats in sorted(counts, key=lambda a: -counts[a]):
        print(f"  {ats:16s} {counts[ats]}")
    print(f"Unresolved ({len(unresolved)}):")
    for entry in unresolved:
        print(f"  - {entry['name']}")


def keep_on_force(entry: dict[str, Any], careers_fallback: bool) -> bool:
    """Whether --force leaves an already-resolved entry alone.

    Pinned entries always survive. So do careers-page resolutions when this run
    has no careers-page stage: slug guessing cannot re-find a board whose token
    was read off a website, so re-probing one only moves a live board back to
    unresolved -- which the monthly `--force` run would otherwise do every month.
    """
    return bool(entry.get("pinned")) or (
        entry.get("source") == "careers_page" and not careers_fallback)


def load_seed_urls(path: Path | None) -> dict[str, str]:
    """company,url CSV -> {lowercased company: url}; missing file means no seeds."""
    if not path or not path.exists():
        return {}
    import csv
    with path.open(newline="") as fh:
        return {r["company"].strip().lower(): r["url"].strip()
                for r in csv.DictReader(fh)
                if (r.get("company") or "").strip() and (r.get("url") or "").strip()}


async def run(
    input_path: Path,
    output_path: Path,
    force: bool,
    careers_fallback: bool = False,
    cache_dir: Path | None = None,
    seed_urls_path: Path | None = None,
) -> None:
    """Full resolver pass: read names, probe, merge with existing, write, summarize."""
    names = read_company_names(input_path)
    existing = load_existing(output_path)
    already_resolved = {
        e["name"]: e
        for e in existing["companies"]
        if e.get("name") in names and (not force or keep_on_force(e, careers_fallback))
    }
    if already_resolved:
        log.info(
            "keeping %d already-resolved companies (%s)",
            len(already_resolved),
            "pinned entries survive --force" if force else "use --force to redo",
        )

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True) as client:
        companies, unresolved = await resolve_all(
            names, already_resolved, client, careers_fallback, cache_dir,
            load_seed_urls(seed_urls_path),
        )

    write_yaml(output_path, companies, unresolved, existing["pinned"])
    print_summary(companies, unresolved)
    print(f"\nWrote {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve ATS slugs for dream companies.")
    parser.add_argument(
        "--force", action="store_true",
        help="re-resolve already-resolved companies (entries marked pinned: true are kept)",
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="company list, one name per line")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="companies.yaml destination")
    parser.add_argument(
        "--careers-fallback", action="store_true",
        help="for companies slug-guessing misses, read their careers page and lift the "
             "real board token from it (also detects Workday). Off by default: it crawls "
             "third-party websites, which the scheduled monthly run should not do.",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
        help="disk cache for fetched careers pages (default: %(default)s)",
    )
    parser.add_argument(
        "--seed-urls", type=Path, default=None,
        help="company,url CSV of known websites, tried first by --careers-fallback "
             "(content-checked like any guessed domain)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.input.exists():
        sys.exit(f"Input file not found: {args.input}")
    asyncio.run(run(args.input, args.output, args.force, args.careers_fallback, args.cache_dir,
                    args.seed_urls))


if __name__ == "__main__":
    main()
