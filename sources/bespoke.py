# ============================================================
# sources/bespoke.py — Bespoke careers pages (PRD §2.10)
# ============================================================
#
# The long tail: companies whose jobs are on their own careers page and nowhere
# we can reach by API. Two kinds, treated identically because both render as
# readable HTML —
#
#   * `bespoke_page`   — no hiring vendor on the page at all
#   * `unsupported_ats:*` — a vendor whose API we cannot read (iCIMS, ADP,
#     Paylocity, JazzHR, Recruitee's careers-page.com white-label, ...)
#
# Reading a page costs a model call, so the whole design is about not making
# that call. Each run:
#
#   1. continues through the company list from a saved cursor, taking as many
#      companies as fit this run's budget (config.BESPOKE_RUN_BUDGET_SECS);
#   2. asks each server whether the page changed, cheapest question first —
#      a conditional GET that comes back 304 costs nothing and ends the check;
#   3. for a 200, hashes the *normalised job region* rather than the page, so
#      a rotating CSRF token or a "generated at" timestamp is not a change;
#   4. extracts only from pages that actually changed, or that have never been
#      extracted at all.
#
# A company counts as done only once it is fully processed (checked, and
# extracted if it needed to be). State and cursor are saved only for done
# companies, and only after main.py has queued their postings -- a run that
# runs out of budget, is abandoned, or dies loses no postings; the unfinished
# companies are simply taken again by the next run.
#
# Everything that comes out goes through the same title pre-filter as every
# other source and is handed to main.py for the location filter, scoring and
# dedup. Nothing here bypasses the funnel.
#
# No company is excluded by size, sector, or whether it has anything posted
# today — a company with nothing open is exactly what this is monitoring for.

from __future__ import annotations

import asyncio
import csv
import hashlib
import html as html_lib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
import llm  # noqa: E402
from llm import ApiAccessError  # noqa: E402
from sources.ats import _title_passes, _work_model  # noqa: E402
from tools.resolve_ats import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    _INITIALISM_STOPWORDS,
    USER_AGENT,
    HostThrottle,
    PageCache,
    RobotsCache,
    _alnum,
    _name_words,
    _normalize_text,
    name_phrase,
)

csv.field_size_limit(10 ** 9)

STATE_PATH = REPO_ROOT / config.BESPOKE_STATE_FILE

# Consecutive extraction failures that mean the API is down rather than the
# pages being bad. Stops a broken key from burning the whole run's budget.
EXTRACTION_ERROR_STREAK = 5


# ---------------------------------------------------------------- company list

def load_companies(csv_path: Path | None = None) -> list[dict[str, str]]:
    """Every unresolved company that has a careers page we could read.

    A row qualifies on having a careers_url, not on its reason: the reason says
    why we could not reach an API, and the URL says we have a page. The
    no_site rows are the ones genuinely out of scope — no page was ever
    confirmed for them, so there is nothing to fetch.
    """
    path = Path(csv_path or config.BESPOKE_SOURCE_CSV)
    if not path.exists():
        print(f"[Bespoke] {path.name} not found — bespoke source inactive")
        return []
    out, seen = [], set()
    parked = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            company = (row.get("company") or "").strip()
            url = (row.get("careers_url") or "").strip()
            reason = (row.get("reason") or "").strip()
            if not company or not url.startswith("http"):
                continue
            if not reason.startswith(config.BESPOKE_REASON_PREFIXES):
                continue
            if _on_skip_host(url):
                parked += 1
                continue
            key = company.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"company": company, "url": url, "reason": reason})
    if parked:
        print(f"[Bespoke] skipped {parked} careers URLs on domain-marketplace hosts "
              f"(config.BESPOKE_SKIP_HOSTS)")
    return out


def _on_skip_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in config.BESPOKE_SKIP_HOSTS)


def order_key(company: str) -> str:
    """A company's stable position in the rolling pass.

    Hashed rather than taken from row order so that rewriting the CSV (which
    the resolver does on every pass) neither reshuffles the pass nor moves the
    cursor: a new company slots in at its hash and is reached in turn.
    """
    return hashlib.sha256(company.strip().lower().encode()).hexdigest()


# ---------------------------------------------------------------------- state

def prior_for(state: dict[str, dict[str, Any]], company: str, url: str) -> dict[str, Any]:
    """A company's stored state, or nothing when its careers URL has moved.

    The etag, Last-Modified, fingerprint and last_extracted all describe the
    page at the stored URL. Carried over to a new URL, If-Modified-Since could
    earn a 304 from a page never read, and the page would look extracted
    already (SRC-17 moved ~250 companies off guessed domains).
    """
    prior = state.get(company.strip().lower(), {})
    if prior.get("url") and prior["url"] != url:
        return {}
    return prior


def load_state(path: Path | None = None) -> dict[str, dict[str, Any]]:
    p = Path(path or STATE_PATH)
    if not p.exists():
        return {}
    try:
        with p.open() as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError) as exc:
        print(f"[Bespoke] state file unreadable ({exc}) — starting empty")
        return {}


def save_state(state: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    p = Path(path or STATE_PATH)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)  # atomic: a killed run never leaves a half-written state


CURSOR_PATH = REPO_ROOT / config.BESPOKE_CURSOR_FILE


def load_cursor(path: Path | None = None) -> dict[str, Any]:
    """{"position": order_key of the last company done, "pass": n, "done_this_pass": n, ...}."""
    p = Path(path or CURSOR_PATH)
    if not p.exists():
        return {"position": "", "pass": 1, "done_this_pass": 0, "passes_completed": 0}
    with p.open() as fh:
        return json.load(fh)


def save_cursor(cursor: dict[str, Any], path: Path | None = None) -> None:
    p = Path(path or CURSOR_PATH)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as fh:
        json.dump(cursor, fh, indent=1, sort_keys=True)
    os.replace(tmp, p)


# ------------------------------------------------------- page normalisation

_SCRIPTISH_RE = re.compile(
    r"<(script|style|noscript|svg|template|iframe)\b[^>]*>.*?</\1>", re.I | re.S)
# Whole structural regions that are page furniture, not listings. Matched
# non-greedily on the *same* tag so a nested <nav> cannot swallow the page.
_CHROME_RE = re.compile(
    r"<(nav|header|footer|aside)\b[^>]*>.*?</\1>", re.I | re.S)
# Consent/newsletter/social blocks, matched on the class or id that names them.
_BANNER_RE = re.compile(
    r"<(div|section|aside)\b[^>]*\b(?:class|id)\s*=\s*[\"'][^\"']*"
    r"(?:cookie|consent|gdpr|newsletter|subscribe|social|breadcrumb|"
    r"announce|promo-banner)[^\"']*[\"'][^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Values that change on every render and mean nothing. Left in, they make every
# page look changed on every check and the hash stops being a cost control.
_VOLATILE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?", re.I),
    re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s?(am|pm)?\b", re.I),
    re.compile(r"\b(?:csrf|nonce|_token|session|request_?verification)[\w-]*\b\S{0,80}", re.I),
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),          # hex tokens / build hashes
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),     # base64-ish blobs
    re.compile(r"\b\d{10,13}\b"),                    # epoch timestamps
    re.compile(r"\b(?:copyright|©)\s*\d{4}\b", re.I),
]

_JOB_PATH_RE = re.compile(
    r"/(?:job|jobs|career|careers|position|positions|opening|openings|"
    r"vacanc(?:y|ies)|role|roles|apply|posting|postings)(?:[/?#]|$)", re.I)
_ANCHOR_RE = re.compile(
    r"""<a\b[^>]*?href\s*=\s*["']([^"'>\s]+)["'][^>]*>(.{0,200}?)</a>""", re.I | re.S)


def visible_text(html: str) -> str:
    """Readable page text: chrome and scripts gone, entities decoded."""
    body = _SCRIPTISH_RE.sub(" ", html or "")
    body = _CHROME_RE.sub(" ", body)
    body = _BANNER_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    return re.sub(r"\s+", " ", html_lib.unescape(body)).strip()


def word_count(text: str) -> int:
    return len(text.split())


def job_anchors(html: str, base_url: str) -> list[tuple[str, str]]:
    """(url, label) for links that look like individual job postings."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, inner in _ANCHOR_RE.findall(html or ""):
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        if not _JOB_PATH_RE.search(href):
            continue
        label = re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner)).strip()
        url = urljoin(base_url, href)
        if not label or url in seen:
            continue
        seen.add(url)
        out.append((url, label))
    return out


def job_region_fingerprint(html: str, base_url: str) -> str:
    """A hash of what a reader would call "the listings", not of the page.

    Two inputs, in order of trust. The job links are the strongest signal —
    when a page has them, an opening appearing or closing changes the set and
    nothing else on the page does. Their absence is why the normalised text is
    kept as the fallback: a page that lists roles as plain headings has no
    links to diff, and hashing nothing would mean never noticing it hire.

    Volatile values are scrubbed either way. Without that, a page carrying a
    CSRF token or a render timestamp reads as changed on every single check,
    and the whole point of the hash is gone.
    """
    anchors = job_anchors(html, base_url)
    if anchors:
        payload = "\n".join(sorted(f"{u}\t{label.lower()}" for u, label in anchors))
    else:
        payload = visible_text(html).lower()
    for pattern in _VOLATILE_RES:
        payload = pattern.sub(" ", payload)
    payload = re.sub(r"\s+", " ", payload).strip()
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]


# ------------------------------------------------------------------ fetching

class CheckResult:
    """What one company's check found."""

    __slots__ = ("company", "url", "status", "changed", "html", "final_url",
                 "etag", "last_modified", "fingerprint", "words", "note")

    def __init__(self, company: str, url: str) -> None:
        self.company, self.url = company, url
        self.status = 0
        self.changed = False
        self.html = ""
        self.final_url = url
        self.etag = ""
        self.last_modified = ""
        self.fingerprint = ""
        self.words = 0
        self.note = ""


async def check_one(client: httpx.AsyncClient, entry: dict[str, str],
                    prior: dict[str, Any], throttle: HostThrottle,
                    semaphore: asyncio.Semaphore, robots: RobotsCache,
                    cache: PageCache) -> CheckResult:
    """Ask one server whether its careers page changed, as cheaply as it allows.

    Deliberately not routed through the resolver's fetch_page: that answers
    from the disk cache first, which is right for resolution and exactly wrong
    here — a cached body can never tell us whether the live page moved. The
    cache is still *written*, so the rest of the tooling keeps benefiting.
    """
    result = CheckResult(entry["company"], entry["url"])
    url = entry["url"]

    if not await robots.allows(client, url, throttle):
        result.status = 999
        result.note = "robots_disallow"
        return result

    headers: dict[str, str] = {}
    if prior.get("etag"):
        headers["If-None-Match"] = prior["etag"]
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = prior["last_modified"]

    async with semaphore:
        await throttle.wait(urlsplit(url).netloc)
        try:
            response = await client.get(url, headers=headers,
                                        timeout=config.BESPOKE_TIMEOUT)
        except Exception as exc:                      # noqa: BLE001
            result.status = -1
            result.note = f"fetch_error: {type(exc).__name__}"
            return result

    result.status = response.status_code
    result.final_url = str(response.url)
    result.etag = response.headers.get("etag", "") or prior.get("etag", "")
    result.last_modified = (response.headers.get("last-modified", "")
                            or prior.get("last_modified", ""))

    # The cheap answer: the server says nothing moved. No body, no hash, no
    # model call. This is the branch the whole design exists to reach.
    if response.status_code == 304:
        result.changed = False
        result.fingerprint = prior.get("fingerprint", "")
        result.words = prior.get("words", 0)
        result.note = "not_modified"
        return result

    if response.status_code != 200 or "html" not in response.headers.get("content-type", "").lower():
        result.note = f"http_{response.status_code}"
        return result

    html = response.text[:config.BESPOKE_MAX_PAGE_BYTES]
    result.html = html
    result.fingerprint = job_region_fingerprint(html, result.final_url)
    result.words = word_count(visible_text(html))
    result.changed = result.fingerprint != prior.get("fingerprint", "")
    result.note = "changed" if result.changed else "same_fingerprint"
    try:
        cache.put(url, 200, html, result.final_url)
    except OSError:
        pass  # the cache is an optimisation for other tools; never fail on it
    return result


async def check_all(entries: list[dict[str, str]],
                    state: dict[str, dict[str, Any]]) -> list[CheckResult]:
    """Conditional-GET every company in this batch, politely and in parallel."""
    throttle = HostThrottle(base_delay=config.BESPOKE_HOST_DELAY)
    semaphore = asyncio.Semaphore(config.BESPOKE_MAX_IN_FLIGHT)
    robots = RobotsCache()
    cache = PageCache(DEFAULT_CACHE_DIR)
    limits = httpx.Limits(max_connections=config.BESPOKE_MAX_IN_FLIGHT + 10,
                          max_keepalive_connections=config.BESPOKE_MAX_IN_FLIGHT)
    headers = {"User-Agent": USER_AGENT,
               "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(timeout=config.BESPOKE_TIMEOUT, headers=headers,
                                 follow_redirects=True, limits=limits) as client:
        return await asyncio.gather(*(
            check_one(client, e, prior_for(state, e["company"], e["url"]),
                      throttle, semaphore, robots, cache)
            for e in entries
        ))


# ---------------------------------------------------------------- extraction

EXTRACT_SYSTEM = """You read one company's careers page and list the jobs on it.

Return ONLY a JSON object, no prose and no markdown fences:
{"jobs": [{"title": "...", "location": "...", "url": "...", "company": "..."}]}

Rules:
- Only list openings that are actually on THIS page. Never infer, complete, or
  invent a job that is not written there.
- "title" must be copied from the page verbatim.
- "location" verbatim if the page gives one, else "".
- "url" must be one of the links supplied below, copied exactly, else "".
- "company" is the employer named on the page. If the page lists jobs at other
  organisations (a portfolio page, a job board, a partner list), put that other
  employer's name here -- do not substitute the site's owner.
- A page with nothing open returns {"jobs": []}. So does a page that is only
  navigation, marketing copy, or a form.
"""


def page_digest(company: str, html: str, url: str) -> str:
    """The prompt payload: readable text plus the links stripping tags threw away."""
    text = visible_text(html)[:config.BESPOKE_MAX_DIGEST_CHARS]
    anchors = job_anchors(html, url)[:config.BESPOKE_MAX_ANCHORS]
    lines = [f"COMPANY: {company}", f"PAGE URL: {url}", "", "PAGE TEXT:", text]
    if anchors:
        lines += ["", "LINKS ON THE PAGE:"]
        lines += [f"- {label} -> {u}" for u, label in anchors]
    return "\n".join(lines)


def company_matches(claimed: str, name: str) -> bool:
    """True if the employer the model named is this company.

    Same test board_belongs_to applies to a scraped board token: normalise to
    letters and digits, accept containment either way, and let an organisation
    that brands by its initials pass on an exact initialism.
    """
    claimed_key, name_key = _alnum(claimed), _alnum(name)
    if not claimed_key:
        return False
    if claimed_key == name_key or claimed_key in name_key or name_key in claimed_key:
        return True
    words = [_alnum(w) for w in _name_words(name) if len(w) >= 5]
    if words and all(w in claimed_key for w in words):
        return True
    # Stopwords are dropped first, exactly as board_belongs_to does it:
    # "Coalition for Green Capital" brands itself CGC, never CFGC.
    significant = [w for w in _name_words(name) if w not in _INITIALISM_STOPWORDS]
    initials = "".join(w[0] for w in significant if w)
    return len(initials) >= 3 and initials == claimed_key


def listing_is_theirs(job: dict[str, str], company: str, page_text_norm: str,
                      hrefs: set[str]) -> str:
    """"" if the listing is really on this page and really this company's.

    Three separate ways an extraction goes wrong and each needs its own test: a
    title the model wrote rather than read, a job belonging to some other
    employer listed on the same page, and a URL assembled from a pattern
    instead of copied. The employer test is the one that matters most — a
    single careers page in testing listed 40+ other firms' openings, and
    without this check every one of them would have been filed under the site's
    owner.
    """
    title = (job.get("title") or "").strip()
    if not title:
        return "no_title"
    if _normalize_text(title) not in page_text_norm:
        return "title_not_on_page"
    claimed = (job.get("company") or "").strip()
    if not claimed:
        return "no_company"
    if not company_matches(claimed, company):
        return "other_employer"
    url = (job.get("url") or "").strip()
    if url and url not in hrefs:
        return "url_not_on_page"
    return ""


def extract_jobs(company: str, html: str, url: str,
                 model: str | None = None) -> tuple[list[dict[str, str]], int, int, str]:
    """One model call over one page. Returns (jobs, in_tokens, out_tokens, error)."""
    model = model or config.BESPOKE_EXTRACT_MODEL
    try:
        body, reply_usage = llm.complete(
            model, EXTRACT_SYSTEM, [{"role": "user", "content": page_digest(company, html, url)}], 4096,
            # REV-09: no cache_system. EXTRACT_SYSTEM is about 233 tokens,
            # under every model's minimum cacheable length (512 at the
            # lowest), so the breakpoint REV-06 added was never honoured.
            json_mode=True, purpose="Bespoke extraction",
            # Bounded so one slow call cannot carry a run past its budget.
            timeout=config.BESPOKE_EXTRACT_TIMEOUT)
    except ApiAccessError:
        # OPS-12: billing/auth/permission ends the whole fetch run red; only
        # transient errors count toward the api_errors streak below.
        raise
    except Exception as exc:                          # noqa: BLE001
        return [], 0, 0, f"api_error: {exc}"

    body = body.strip()
    usage = (reply_usage.input_tokens, reply_usage.output_tokens)
    body = re.sub(r"^```(?:json)?|```$", "", body, flags=re.M).strip()
    try:
        parsed = json.loads(body)
        jobs = [j for j in parsed.get("jobs", []) if isinstance(j, dict)]
    except (ValueError, AttributeError):
        return [], usage[0], usage[1], "unparseable_response"
    return jobs, usage[0], usage[1], ""


def to_job_dicts(raw: Iterable[dict[str, str]], company: str, html: str,
                 page_url: str, counters: dict[str, int]) -> list[dict[str, str]]:
    """Content-check, then title pre-filter, then shape like every other source.

    Category headings ("ROLES WE HIRE", "Software & ML") do come through here as
    listings — they are verbatim on the page and correctly attributed, so the
    content check cannot see them. They are left alone deliberately: the title
    pre-filter drops them one line later, and a heuristic aimed at them would
    be one more thing to maintain and to get wrong.
    """
    page_text_norm = _normalize_text(visible_text(html))
    hrefs = {u for u, _ in job_anchors(html, page_url)}
    out = []
    for job in raw:
        verdict = listing_is_theirs(job, company, page_text_norm, hrefs)
        if verdict:
            counters[f"rejected_{verdict}"] += 1
            continue
        counters["content_check_ok"] += 1
        title = (job.get("title") or "").strip()
        if not _title_passes(title):
            counters["dropped_title_filter"] += 1
            continue
        counters["passed_title_filter"] += 1
        location = (job.get("location") or "").strip()
        out.append({
            "title": title,
            "company": company,
            "location": location,
            "work_model": _work_model(location),
            "comp": "",
            "apply_link": (job.get("url") or "").strip() or page_url,
            "description": "",
            "date_posted": "",  # bespoke pages carry no reliable posted date
            "posted_precision": "",
            "source": "bespoke",
            "tier": "bespoke",
            "is_dream_company": False,
        })
    return out


# --------------------------------------------------------------------- driver

def _api_ready() -> bool:
    """True if this run can actually call the model API.

    Without this check, a run with no key spends the whole extraction budget
    on calls that each fail identically.
    """
    if not llm.available():
        print("[Bespoke] no API credentials resolved — extraction skipped this run")
        return False
    return True


def _new_stats(companies: int) -> dict[str, Any]:
    return {
        "companies": companies, "not_modified_304": 0, "same_fingerprint": 0,
        "changed": 0, "never_extracted": 0, "js_only": 0, "errors": 0,
        "robots_disallow": 0, "extracted": 0, "extraction_errors": 0,
        "listings_raw": 0, "in_tokens": 0, "out_tokens": 0,
        "fetch_seconds": 0.0, "extract_seconds": 0.0,
    }


def _new_counters() -> dict[str, int]:
    return {k: 0 for k in (
        "content_check_ok", "passed_title_filter", "dropped_title_filter",
        "rejected_no_title", "rejected_title_not_on_page", "rejected_no_company",
        "rejected_other_employer", "rejected_url_not_on_page")}


def record_check(r: CheckResult, prior: dict[str, Any], today: str,
                 stats: dict[str, Any], js_only: list) -> tuple[dict[str, Any], bool]:
    """The state entry a check produces, and whether the page needs extracting.

    Pure with respect to the stored state: the caller decides when the entry
    is committed.
    """
    entry = dict(prior)
    entry.update(url=r.url, last_checked=today, last_status=r.status, note=r.note)
    if r.etag:
        entry["etag"] = r.etag
    if r.last_modified:
        entry["last_modified"] = r.last_modified

    if r.status == 999:
        stats["robots_disallow"] += 1
        return entry, False
    if r.status == 304:
        stats["not_modified_304"] += 1
        return entry, False
    if r.status != 200:
        stats["errors"] += 1
        return entry, False

    entry["fingerprint"] = r.fingerprint
    entry["words"] = r.words
    if r.words < config.BESPOKE_MIN_VISIBLE_WORDS:
        stats["js_only"] += 1
        js_only.append((r.company, r.final_url, r.words))
    never = not prior.get("last_extracted")
    # Changed and never-extracted are independent facts and both drive
    # extraction, so they are counted independently. Rolling one into
    # the other made an unchanged page that had never been read look
    # like a page that had moved.
    if r.changed:
        stats["changed"] += 1
    else:
        stats["same_fingerprint"] += 1
    if never:
        stats["never_extracted"] += 1
    # A JS shell has nothing to read; paying to confirm that on every
    # change would be the one avoidable cost in the design.
    readable = r.words >= config.BESPOKE_MIN_VISIBLE_WORDS
    return entry, readable and (r.changed or never)


def run(entries: list[dict[str, str]] | None = None,
        state: dict[str, dict[str, Any]] | None = None,
        extract: bool = True, model: str | None = None,
        state_path: Path | None = None,
        max_extractions: int | None = None) -> dict[str, Any]:
    """Check a set of companies and extract from the ones that moved.

    Returns a stats dict; the caller decides what to do with `jobs`. Used by the
    backfill and measurement CLI, which want every company in one go with no
    budget. The fetch runs use run_rolling() instead.
    """
    entries = entries if entries is not None else load_companies()
    state = state if state is not None else load_state(state_path)
    max_extractions = 10 ** 9 if max_extractions is None else max_extractions

    started = time.monotonic()
    results = asyncio.run(check_all(entries, state))
    fetch_seconds = time.monotonic() - started

    stats = _new_stats(len(entries))
    stats["fetch_seconds"] = round(fetch_seconds, 1)
    counters = _new_counters()
    js_only: list[tuple[str, str, int]] = []
    to_extract: list[CheckResult] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for r in results:
        entry, needs = record_check(r, prior_for(state, r.company, r.url), today, stats, js_only)
        if needs:
            to_extract.append(r)
        state[r.company.lower()] = entry

    if to_extract:
        if not (extract and _api_ready()):
            # Either extraction was turned off for this pass, or there is no
            # API credentials. Both mean the same thing here: record what we
            # would have read and make no calls. Leaving the queue populated
            # would drive the loop below with no credentials and log every
            # company as an API error.
            stats["extraction_deferred"] = len(to_extract)
            to_extract = []

    jobs: list[dict[str, str]] = []
    started = time.monotonic()
    consecutive_errors = 0
    for r in to_extract[:max_extractions]:
        raw, tin, tout, err = extract_jobs(r.company, r.html, r.final_url, model)
        stats["in_tokens"] += tin
        stats["out_tokens"] += tout
        if err:
            stats["extraction_errors"] += 1
            consecutive_errors += 1
            # A run of identical failures is the API being unreachable, not
            # these particular pages. Grinding through the rest of the queue
            # only delays the digest and buries the real cause.
            if consecutive_errors >= EXTRACTION_ERROR_STREAK:
                stats["extraction_aborted"] = True
                print(f"[Bespoke] {consecutive_errors} extractions failed in a row "
                      f"({err}) — stopping extraction for this run")
                break
            continue
        consecutive_errors = 0
        stats["extracted"] += 1
        stats["listings_raw"] += len(raw)
        jobs += to_job_dicts(raw, r.company, r.html, r.final_url, counters)
        state[r.company.lower()]["last_extracted"] = today
    stats["extract_seconds"] = round(time.monotonic() - started, 1)
    stats["deferred_over_cap"] = max(0, len(to_extract) - max_extractions)

    save_state(state, state_path)
    stats.update(counters)
    stats["jobs"] = jobs
    stats["js_only_list"] = js_only
    return stats


def _extract_tracked(r: CheckResult, model: str | None,
                     live_streak: list[int]) -> tuple[list[dict[str, str]], int, int, str]:
    """extract_jobs for a worker thread, keeping a live error streak.

    The streak lets the submitting loop stop starting new calls during an API
    outage; the authoritative streak is recomputed in company order afterwards.
    """
    try:
        raw, tin, tout, err = extract_jobs(r.company, r.html, r.final_url, model)
    except ApiAccessError:
        # Stop the submitting loop starting more calls; pass 2 re-raises this
        # from future.result() and the run ends before anything is committed.
        live_streak[0] = EXTRACTION_ERROR_STREAK
        raise
    live_streak[0] = live_streak[0] + 1 if err else 0
    return raw, tin, tout, err


class RollingRun:
    """One budgeted slice of the rolling pass, not yet committed."""

    def __init__(self) -> None:
        self.jobs: list[dict[str, str]] = []
        self.entries: dict[str, dict[str, Any]] = {}  # company key -> state entry, done companies only
        self.cursor: dict[str, Any] = {}
        self.stats: dict[str, Any] = _new_stats(0)
        self.counters = _new_counters()
        self.stop_reason = ""


def run_rolling(companies: list[dict[str, str]], state: dict[str, dict[str, Any]],
                cursor: dict[str, Any], budget_secs: float,
                cancel: threading.Event | None = None, extract: bool = True,
                model: str | None = None, api_ready: bool | None = None, clock=time.monotonic,
                check=None, max_extractions: int | None = None) -> RollingRun:
    """Continue the rolling pass from `cursor` for as long as the budget allows.

    Companies are taken in order_key order, starting after the cursor and
    wrapping round at the end (which completes a pass). They are checked in
    batches; each company is then *done* only once it needs nothing more --
    unchanged, unreadable, errored, or extracted. The cursor advances over done
    companies only and never skips one, so the first company left unfinished
    (budget, an API outage, cancellation) is where the next run
    resumes. Nothing is written here: the caller commits entries and cursor
    after it has queued the jobs.

    REV-06: at most `max_extractions` extraction calls (None: no cap). The
    first company that would need one more stops the run (stop_reason
    "extraction_cap") before it counts as done, so the cursor stays on it and
    the next pass starts there. stats["extraction_calls"] counts the calls.
    """
    out = RollingRun()
    check = check or (lambda batch: asyncio.run(check_all(batch, state)))
    deadline = clock() + budget_secs
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ordered = sorted(companies, key=lambda c: order_key(c["company"]))
    keys = [order_key(c["company"]) for c in ordered]
    position = cursor.get("position", "")
    start = next((i for i, k in enumerate(keys) if k > position), 0) if position else 0
    queue = ordered[start:] + ordered[:start]

    new_cursor = {"position": position, "pass": cursor.get("pass", 1),
                  "done_this_pass": cursor.get("done_this_pass", 0),
                  "passes_completed": cursor.get("passes_completed", 0),
                  "companies": len(ordered)}
    stats, counters, js_only = out.stats, out.counters, []
    stats["companies"] = 0
    live_streak = [0]   # consecutive extraction errors as calls finish, shared with workers
    stats["extraction_calls"] = 0
    cap = None if max_extractions is None else max(0, int(max_extractions))
    last_key = keys[-1] if keys else ""

    i = 0
    while i < len(queue):
        if cancel is not None and cancel.is_set():
            out.stop_reason = "cancelled"
            break
        if clock() >= deadline - config.BESPOKE_BATCH_RESERVE_SECS:
            out.stop_reason = "budget"
            break
        batch = queue[i:i + config.BESPOKE_CHECK_BATCH]
        started = clock()
        results = check(batch)
        stats["fetch_seconds"] = round(stats["fetch_seconds"] + clock() - started, 1)

        # Pass 1, in company order: record each check and start extractions,
        # up to BESPOKE_EXTRACT_CONCURRENCY at a time. A new extraction starts
        # only while there is room left in the budget; the first company that
        # cannot start marks where this run stops.
        records = []          # (result, entry, future or None) in company order
        stop = False
        started = clock()
        with ThreadPoolExecutor(max_workers=config.BESPOKE_EXTRACT_CONCURRENCY) as pool:
            running = set()
            for r in results:
                if cancel is not None and cancel.is_set():
                    out.stop_reason, stop = "cancelled", True
                    break
                key = r.company.lower()
                entry, needs = record_check(r, prior_for(state, r.company, r.url), today, stats, js_only)
                future = None
                if needs and extract:
                    if live_streak[0] >= EXTRACTION_ERROR_STREAK:
                        out.stop_reason, stop = "api_errors", True
                        break
                    if api_ready is None:
                        api_ready = _api_ready()
                    if not api_ready:
                        out.stop_reason, stop = "no_api_client", True
                        break
                    if cap is not None and stats["extraction_calls"] >= cap:
                        out.stop_reason, stop = "extraction_cap", True
                        break
                    while len(running) >= config.BESPOKE_EXTRACT_CONCURRENCY:
                        finished, running = wait(running, return_when=FIRST_COMPLETED)
                    if clock() >= deadline - config.BESPOKE_EXTRACT_RESERVE_SECS:
                        out.stop_reason, stop = "budget", True
                        break
                    future = pool.submit(_extract_tracked, r, model, live_streak)
                    running.add(future)
                    stats["extraction_calls"] += 1
                records.append((r, entry, future))
            wait(running)  # each call is bounded by BESPOKE_EXTRACT_TIMEOUT
        if any(f is not None for _, _, f in records):
            stats["extract_seconds"] = round(stats["extract_seconds"] + clock() - started, 1)

        # Pass 2, in company order again: a company is done only if every
        # company before it in this batch is done. A run of API errors stops
        # the pass at the company that hit the streak limit.
        streak = 0
        for r, entry, future in records:
            key = r.company.lower()
            if future is not None:
                raw, tin, tout, err = future.result()
                stats["in_tokens"] += tin
                stats["out_tokens"] += tout
                if err:
                    stats["extraction_errors"] += 1
                    stats["last_error"] = err
                    streak += 1
                    if streak >= EXTRACTION_ERROR_STREAK:
                        # The API, not this page: stop here so the pass does
                        # not skip a run of companies it never read.
                        stats["extraction_aborted"] = True
                        out.stop_reason, stop = "api_errors", True
                        break
                    # One bad page: done for now, still never-extracted, so
                    # the next pass tries it again.
                else:
                    streak = 0
                    stats["extracted"] += 1
                    stats["listings_raw"] += len(raw)
                    out.jobs += to_job_dicts(raw, r.company, r.html, r.final_url, counters)
                    entry["last_extracted"] = today
            out.entries[key] = entry
            stats["companies"] += 1
            k = order_key(r.company)
            new_cursor["position"] = k
            new_cursor["done_this_pass"] += 1
            if k == last_key:
                new_cursor["passes_completed"] += 1
                new_cursor["pass"] += 1
                new_cursor["done_this_pass"] = 0
                new_cursor["position"] = ""
        if stop:
            break
        i += len(batch)

    stats["js_only_list"] = js_only
    stats.update(counters)
    new_cursor["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_cursor["last_stop"] = out.stop_reason or "list_end"
    out.cursor = new_cursor
    return out


class BespokeJobs(list):
    """Bespoke's jobs, plus commit(): save state and cursor for the done companies.

    main.py calls commit() only after it has queued these jobs for the digest,
    so a posting is never marked read unless it is safely queued. An abandoned
    run is cancelled first, and a cancelled run's commit() does nothing.
    """

    def __init__(self, jobs=(), commit=None) -> None:
        super().__init__(jobs)
        self._commit = commit

    def commit(self) -> None:
        if self._commit is not None:
            self._commit()


_CANCEL = threading.Event()


def cancel() -> None:
    """Called by main.run_source when this source is abandoned over budget."""
    _CANCEL.set()


def fetch_all():
    """Entry point for main.py — the next budgeted slice of the rolling pass."""
    if not getattr(config, "BESPOKE_ENABLED", False):
        print("[Bespoke] disabled in config")
        return []

    _CANCEL.clear()
    companies = load_companies()
    if not companies:
        return []
    state = load_state()
    cursor = load_cursor()
    # REV-06: the per-run extraction cap, and what is left of the day's
    # model-call ceiling (scoring.max_calls_per_day), whichever is lower.
    import fetch_budget
    import profile_loader
    per_run = ((profile_loader.active()["sources"].get("bespoke") or {}).get("max_extractions_per_run")
               or config.BESPOKE_MAX_EXTRACTIONS_PER_RUN)
    daily_left = fetch_budget.remaining_today()
    cap = min(per_run, daily_left)
    result = run_rolling(companies, state, cursor, config.BESPOKE_RUN_BUDGET_SECS, cancel=_CANCEL,
                         max_extractions=cap)
    stats, new = result.stats, result.cursor
    fetch_budget.RUN["extractions"] += stats.get("extraction_calls", 0)
    if result.stop_reason == "extraction_cap":
        why = (f"sources.bespoke.max_extractions_per_run {per_run}" if per_run <= daily_left
               else f"the daily ceiling scoring.max_calls_per_day {fetch_budget.ceiling()}, "
                    f"{daily_left} calls left today")
        print(f"[Bespoke] extraction cap reached after {stats['extraction_calls']} extractions ({why}); "
              f"the rest of the pages wait for the next pass, cursor at the first one left")
    print(f"[Bespoke] {stats.get('extraction_calls', 0)} extraction calls this run (cap {cap})")

    print(f"[Bespoke] pass {cursor.get('pass', 1)}: {stats['companies']} of {len(companies)} companies done "
          f"this run (stopped: {result.stop_reason or 'end of list'}); "
          f"{new['done_this_pass']} done in pass {new['pass']}"
          f"{'; completed a full pass' if new['passes_completed'] > cursor.get('passes_completed', 0) else ''}")
    print(f"[Bespoke] 304={stats['not_modified_304']} unchanged={stats['same_fingerprint']} "
          f"changed={stats['changed']} never_extracted={stats['never_extracted']} "
          f"js_only={stats['js_only']} errors={stats['errors']}")
    print(f"[Bespoke] extracted from {stats['extracted']} pages "
          f"({stats['listings_raw']} raw listings, "
          f"{stats['content_check_ok']} passed the content check, "
          f"{stats['passed_title_filter']} passed the title filter) "
          f"in {stats['fetch_seconds']}s fetch + {stats['extract_seconds']}s extract")
    if stats["extraction_errors"]:
        print(f"[Bespoke] {stats['extraction_errors']} extraction errors"
              + (" — extraction aborted, pass resumes at that company" if stats.get("extraction_aborted") else ""))
    warning = api_errors_warning(result)
    if warning:
        # OPS-12: a streak of transient API errors keeps the run green, but
        # shows as a warning in Actions and again in main.py's fetch summary.
        print(f"::warning title=Bespoke extraction stopped::{warning}")
    if stats["rejected_other_employer"]:
        print(f"[Bespoke] blocked {stats['rejected_other_employer']} listings "
              f"belonging to another employer")

    def commit() -> None:
        if _CANCEL.is_set():
            print("[Bespoke] run was abandoned — state and cursor NOT saved; "
                  "the next run redoes these companies")
            return
        latest = load_state()
        latest.update(result.entries)
        save_state(latest)
        save_cursor(new)
        print(f"[Bespoke] saved state for {len(result.entries)} companies; cursor at pass "
              f"{new['pass']}, {new['done_this_pass']} done")

    jobs = BespokeJobs(result.jobs, commit)
    jobs.warning = warning
    return jobs


def api_errors_warning(result: RollingRun) -> str:
    """One line for an api_errors stop: the error count and the last error, else ""."""
    if result.stop_reason != "api_errors":
        return ""
    last = " ".join(str(result.stats.get("last_error", "")).split())[:300]
    return (f"stopped after {result.stats['extraction_errors']} API errors "
            f"({EXTRACTION_ERROR_STREAK} in a row); last error: {last}")


fetch_all.cancel = cancel


def _cli() -> None:
    """Backfill and measurement entry point — not used by the daily run.

    Processes every company (or --limit of them) in one go, ignoring the
    rolling cursor, the run budget and the per-run extraction cap -- what a
    one-time first pass over every company needs and a fetch run must never do.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Bespoke careers-page source.")
    ap.add_argument("--backfill", action="store_true",
                    help="accepted for compatibility; every company is always processed")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-extract", action="store_true",
                    help="change detection only; makes no model calls")
    ap.add_argument("--model", default=None)
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--passes", type=int, default=1,
                    help="repeat the whole pass N times, to measure how much "
                         "re-extraction a second identical run triggers")
    ap.add_argument("--js-only-out", type=Path, default=None,
                    help="write the JavaScript-only pages to this CSV")
    args = ap.parse_args()

    companies = load_companies()
    if args.limit:
        companies = companies[:args.limit]
    print(f"[Bespoke] {len(companies)} companies, {args.passes} pass(es)")

    for n in range(1, args.passes + 1):
        started = time.monotonic()
        stats = run(companies, extract=not args.no_extract, model=args.model,
                    state_path=args.state, max_extractions=10 ** 9)
        jobs = stats.pop("jobs")
        js_only = stats.pop("js_only_list")
        live = stats["companies"] - stats["errors"] - stats["robots_disallow"]
        print(f"\n--- pass {n} ({time.monotonic() - started:.0f}s wall) ---")
        for k in ("companies", "not_modified_304", "same_fingerprint", "changed",
                  "never_extracted", "js_only", "errors", "robots_disallow",
                  "extracted", "extraction_errors", "listings_raw",
                  "content_check_ok", "rejected_other_employer",
                  "rejected_title_not_on_page", "passed_title_filter",
                  "in_tokens", "out_tokens", "fetch_seconds", "extract_seconds"):
            print(f"  {k:28s} {stats.get(k, 0)}")
        print(f"  {'reachable pages':28s} {live}")
        print(f"  {'would extract next run':28s} {stats['changed']}")
        print(f"  {'jobs after title filter':28s} {len(jobs)}")
        if args.js_only_out and js_only:
            with args.js_only_out.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["company", "careers_url", "visible_words"])
                w.writerows(js_only)
            print(f"  wrote {len(js_only)} JavaScript-only pages to {args.js_only_out}")


if __name__ == "__main__":
    _cli()
