# profiles.py — DEL-14: company profiles for the digest blurb
#
# Before the digest is built, each emailed role's company is read once from its
# own website (homepage plus up to two about / careers / values pages), turned
# into a short profile by one Claude call, and cached per domain for
# config.PROFILE_TTL_DAYS. The blurb's company fragment and culture keywords are
# then rewritten from that profile plus the posting, one call per emailed role.
#
# Never blocks delivery: a company with no site, a site that cannot be read, a
# failed call or a blown budget leaves the DEL-13 blurb as it is, with one log
# line per company. Profiles never feed the score. A cache hit makes no network
# request and no profile call. The cache file is written by the digest only.

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

import config
import llm
import profile_loader
from location import join_rationale, split_rationale
from state import employer_name, write_json_atomic
from url_resolver import link_class

REPO_ROOT = Path(__file__).resolve().parent
CACHE_PATH = REPO_ROOT / config.PROFILE_CACHE_FILE

# $ per million tokens (input, output), for the run-log cost line only (PUB-12:
# the one table, config.MODEL_PRICES).
PRICES = config.MODEL_PRICES
# The blurb rewrite uses the scoring model (profile scoring.model), so the
# voice matches the scoring call's blurb. A model missing from PRICES counts as
# $0 in the cost line.
BLURB_MODEL = profile_loader.active()["scoring"]["model"]

# Statuses that are a property of the site, cached for PROFILE_EMPTY_TTL_DAYS.
# Anything else (timeouts, server errors, API errors) is not cached: the next
# digest tries again.
_EMPTY_STATUSES = ("no text", "blocked", "robots", "not this company", "name not on homepage")


# --------------------------------------------------------------- resolution

def profile_name(job):
    """The employer to profile, or "" when the posting does not name one.

    A staffing posting whose client the scoring call could not name has
    hiring_company set and blank: profiling the staffing firm would describe
    the wrong company. A job scored before SCO-06 has no hiring_company and
    falls back to its Company value."""
    if "hiring_company" in job and not str(job.get("hiring_company") or "").strip():
        return ""
    return employer_name(str(job.get("hiring_company") or job.get("company") or ""))


# A legal suffix the posting carries and the harvest may not ("Caterpillar Inc."
# vs "Caterpillar"). Tried only after the full name finds nothing.
_LEGAL_SUFFIX_RE = re.compile(
    r"[,\s]+(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|plc|gmbh|"
    r"s\.?a|n\.?v|ag|lp|llp)\.?$", re.I)


def name_variants(name):
    """The name as given, then without a trailing legal suffix."""
    name = " ".join((name or "").split())
    bare = _LEGAL_SUFFIX_RE.sub("", name).strip()
    return [name] + ([bare] if bare and bare != name else [])


def site_origin(url):
    """https://host for a URL or bare domain, or "" when it has no host."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = (parts.hostname or "").lower()
    return f"{parts.scheme or 'https'}://{host}" if host else ""


def site_domain(url):
    """The cache key: the host without "www."."""
    host = (urlsplit(site_origin(url)).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _own_site(url):
    """A URL that is the company's own site: not an ATS, job board,
    aggregator or LinkedIn host, and not a domain marketplace."""
    from sources.bespoke import _on_skip_host
    origin = site_origin(url)
    return bool(origin) and link_class(origin) == "company" and not _on_skip_host(origin)


class Sites:
    """Resolves a company's website: the harvest first, then
    data/companies.yaml, then the apply link when it is the company's own,
    then (DEL-15) the domain the scoring call named, which fetch_site only
    reads once the homepage names the company."""

    def __init__(self):
        try:
            from sourcing.harvest import website_lookup
            self._harvest = website_lookup()
        except Exception as e:                       # noqa: BLE001
            print(f"[Profile] harvest unavailable ({type(e).__name__}: {e}); "
                  "resolving from companies.yaml and apply links only")
            self._harvest = None
        self._yaml = {}
        try:
            import yaml
            data = yaml.safe_load((REPO_ROOT / "data" / "companies.yaml").read_text()) or {}
            for entry in data.get("companies") or []:
                name = str(entry.get("name") or "").strip().lower()
                if name and entry.get("careers_url"):
                    self._yaml.setdefault(name, entry["careers_url"])
        except Exception as e:                       # noqa: BLE001
            print(f"[Profile] companies.yaml unavailable ({type(e).__name__}: {e})")

    def candidates(self, name, apply_link="", website=""):
        """Every site found for the company, in the order they are tried:
        [(site origin, method)], one per domain. The step moves to the next
        only when one fails (DEL-15)."""
        found, domains = [], set()

        def add(url, method):
            if url and _own_site(url) and site_domain(url) not in domains:
                domains.add(site_domain(url))
                found.append((site_origin(url), method))

        # No crunchbase slug travels with a posting, so the harvest is matched
        # on the name, and only when exactly one harvest company has it (SRC-19).
        for variant in name_variants(name):
            if self._harvest:
                add(self._harvest("", variant), "harvest")
            add(self._yaml.get(variant.lower()), "companies.yaml")
        add(apply_link, "apply link")
        add(website, SCORING_CALL)
        return found

    def resolve(self, name, apply_link="", website=""):
        """(site origin, method) of the first candidate, or ("", "no site found")."""
        found = self.candidates(name, apply_link, website)
        return found[0] if found else ("", "no site found")


# DEL-15: the method for a domain the scoring call named. Never trusted until
# the homepage names the company (homepage_names).
SCORING_CALL = "scoring call"
NAME_NOT_ON_HOMEPAGE = "name not on homepage"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _words(text):
    """Lower case, every run of non-letters / digits one space, padded, so a
    name matches on whole words: "K&L Gates" -> " k l gates "."""
    return " " + " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split()) + " "


# Trailing words that qualify a name rather than make it: a region, an
# organisational form, a division ("RWE Americas", "Planned Parenthood
# Federation of America", "Nestlé IT & Digital Americas"). Country names are
# not here: "Standard Bank Egypt" is not "Standard Bank".
_QUALIFIERS = {
    "america", "americas", "north", "south", "latin", "us", "usa", "u.s", "u.s.a",
    "united", "states", "global", "international", "worldwide", "federation",
    "group", "holdings", "company", "companies", "corporation", "services",
    "it", "digital",
}
# Joining words, stripped only between qualifiers: "Federation of America"
# goes, but "Bank of America" never becomes "Bank".
_CONNECTORS = {"of", "the", "and", "&", "-"}


def leading_words(name):
    """Each name variant with its trailing qualifiers removed, when that
    leaves something shorter with at least 3 letters or digits."""
    out = []
    for variant in name_variants(name):
        words = variant.replace(",", " ").split()
        full = len(words)
        last = ""
        while words and words[-1].casefold().rstrip(".") in _QUALIFIERS | _CONNECTORS:
            last = words.pop().casefold()
        if last in _CONNECTORS:          # the name itself ends "... of America"
            continue
        lead = " ".join(words)
        if len(words) < full and len(re.sub(r"\W", "", lead)) >= 3 and lead not in out:
            out.append(lead)
    return out


def homepage_names(name, html, text):
    """True when the page title or visible text names the company, whole
    words, case and punctuation ignored: the full posted name (any of
    name_variants) or, failing that, its leading words (leading_words)."""
    import html as html_lib
    title = " ".join(html_lib.unescape(m) for m in _TITLE_RE.findall(html or ""))
    page = _words(f"{title} {text}")
    names = name_variants(name) + leading_words(name)
    return any(_words(v).strip() and _words(v) in page for v in names)


# -------------------------------------------------------------------- fetch

def _headers():
    from sources.bespoke import USER_AGENT
    return {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}


def _timeout(deadline):
    return max(0.5, min(config.BESPOKE_TIMEOUT, deadline - time.monotonic()))


def _robots(client, origin, deadline):
    """A parser for the site's robots.txt; an unreadable one allows all."""
    parser = RobotFileParser()
    try:
        r = client.get(f"{origin}/robots.txt", timeout=min(3.0, _timeout(deadline)))
        parser.parse(r.text.splitlines() if r.status_code == 200 else [])
    except httpx.HTTPError:
        parser.parse([])
    return parser


def fetch_site(client, origin, deadline, verify_name=""):
    """The homepage plus the first PROFILE_MAX_SUBPAGES pages of
    config.PROFILE_PATHS that answer 200 with real text.

    Returns {"status", "pages": [{"url", "chars"}], "text"}. Status is "ok"
    when any page had PROFILE_MIN_PAGE_CHARS of visible text.

    With verify_name (DEL-15, a domain the scoring call named), nothing past
    the homepage is read unless the homepage names the company: a homepage
    that cannot be read, or does not carry the name in its title or visible
    text, ends the fetch with no text."""
    from sources.bespoke import USER_AGENT, _on_skip_host, visible_text
    pages, texts, statuses = [], [], []
    robots = _robots(client, origin, deadline)

    def get(url):
        if time.monotonic() > deadline - 0.5:
            statuses.append("budget")
            return None
        if not robots.can_fetch(USER_AGENT, url):
            statuses.append("robots")
            return None
        try:
            r = client.get(url, timeout=_timeout(deadline))
        except httpx.TimeoutException:
            statuses.append("timeout")
            return None
        except httpx.HTTPError as e:
            statuses.append(f"error: {type(e).__name__}")
            return None
        if r.status_code in (401, 403, 429):
            statuses.append("blocked")
            return None
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            statuses.append(f"http {r.status_code}")
            return None
        if _on_skip_host(str(r.url)):
            statuses.append("parked domain")
            return None
        text = visible_text(r.text[:config.BESPOKE_MAX_PAGE_BYTES])
        pages.append({"url": str(r.url), "chars": len(text)})
        if len(text) < config.PROFILE_MIN_PAGE_CHARS:
            statuses.append("no text")
            return None
        return r, text

    home = get(origin + "/")
    seen = set()
    if verify_name and not (home and homepage_names(
            verify_name, home[0].text[:config.BESPOKE_MAX_PAGE_BYTES], home[1])):
        status = NAME_NOT_ON_HOMEPAGE if home else (statuses[0] if statuses else "no pages")
        return {"status": status, "pages": pages, "text": ""}
    if home:
        response, text = home
        texts.append(text)
        seen.add(str(response.url).rstrip("/"))
        # A redirect (example.com -> www.example.com) moves every path with it.
        origin = site_origin(str(response.url)) or origin
    good = 0
    for path in config.PROFILE_PATHS:
        if good >= config.PROFILE_MAX_SUBPAGES or time.monotonic() > deadline - 0.5:
            break
        hit = get(origin + path)
        if not hit:
            continue
        response, text = hit
        final = str(response.url).rstrip("/")
        if final in seen:            # redirected back to a page already read
            continue
        seen.add(final)
        texts.append(text)
        good += 1

    if texts:
        status = "ok"
    elif "blocked" in statuses:
        status = "blocked"
    elif "robots" in statuses:
        status = "robots"
    elif "no text" in statuses:
        status = "no text"
    else:
        status = statuses[0] if statuses else "no pages"
    return {"status": status, "pages": pages,
            "text": "\n\n".join(texts)[:config.PROFILE_MAX_TEXT_CHARS]}


# ------------------------------------------------------------------- Claude

# Bumped whenever the profile's fields change meaning; an entry from another
# version is a cache miss. 2: "culture" (how the company operates and treats
# people) split from the product facts; v1's "operates" mixed the two.
PROFILE_VERSION = 2

PROFILE_SYSTEM = """You read text from one company's own website and write a short
factual profile of it. Return ONLY valid JSON, no prose, no markdown fences.

Use only what the pages say. Never guess, never grade, no marketing phrasing.
Use null for anything the pages do not state.

Keep two things apart:
- The PRODUCT side: what they make or do, for whom, how big, their mission.
  Product features, technology, platforms, markets and sales claims ("AI-first",
  "cloud-native", "open formats", "enterprise-grade", "trusted by 4,000 teams")
  belong here, never in "culture".
- The CULTURE side: how the company operates and treats people: stated values,
  ways of working, remote / hybrid / office stance, how they treat employees,
  stated commitments (to inclusion, transparency, development, community).
  If the pages are all product or sales copy, "culture" is an empty list.

{
  "company_matches": <true if these pages are the named company's own site, false if they are clearly another company, a parked domain or a placeholder>,
  "what": "<what they make or do, a short fragment>",
  "who_for": "<who their customers or users are, a short fragment, or null>",
  "size": "<headcount, stage or scale only if the pages state it, or null>",
  "mission": "<their stated mission, a short fragment, or null>",
  "culture": ["<up to 8 keywords for how they operate and treat people; [] if the pages give none>"]
}"""

REWRITE_SYSTEM = """You rewrite two parts of a short digest blurb for one job posting,
sent to an IT executive. Return ONLY valid JSON, no prose, no markdown fences.

You get the blurb's current company fragment and culture keywords, a word
budget for the two together, the
company's product facts and culture keywords read from its own website, and
the posting. The role part of the blurb is not yours to change and is not
shown to you.
- "company": at most 10 words, a fragment: size, what they make, who for.
  Never the company's name; the card shows it. Prefer the website's facts.
- "culture": at most 8 words, keywords only, without the "Culture:" label:
  how the company operates and treats people (values, ways of working, remote /
  office stance, stated commitments), taken from the website's culture
  keywords and the posting's own about / values text. Never what the product
  is or does: no product features, technology, platforms, markets or
  customers. With no such signal in either, an empty string.
State what is there; never grade it: no evaluative asides. Never mention
compensation or pay. Never use scoring language: no "bonus", "penalty",
"scoring criteria", points, or field values such as leads_department.

Never exceed the word budget: company and culture keywords together, counted
as words separated by spaces. Cut culture keywords before company facts.

{"company": "<fragment>", "culture": "<keywords, or empty>"}"""

# The blurb's target length (DEL-13: 30 to 40 words). The role is fixed text,
# so company + culture get what is left of it.
BLURB_TARGET_WORDS = 40

NO_CULTURE = "no signal in posting or site."

# Abbreviations whose period does not end the company fragment ("U.S. Navy").
_ABBREVIATIONS = {"inc", "co", "corp", "ltd", "st", "dr", "mr", "ms", "vs", "no",
                  "approx", "dept", "govt", "e.g", "i.e"}


def split_blurb(blurb):
    """(company, role, culture) from a DEL-13 blurb "<company>. <role>
    Culture: <keywords>", or None when it does not have that shape.

    The company fragment ends at its first sentence end; a period after a
    single letter (U.S.) or a known abbreviation is not one. The role is
    everything between it and "Culture:", kept exactly as it is."""
    head, sep, culture = (blurb or "").partition(" Culture:")
    if not sep:
        return None
    for m in re.finditer(r"\.\s+", head):
        before = head[:m.start()]
        last = before.split()[-1].lower() if before.split() else ""
        if re.fullmatch(r"[a-z]", last) or last in _ABBREVIATIONS or re.search(r"\b[A-Za-z]\.[A-Za-z]$", before):
            continue
        company, role = head[:m.start() + 1].strip(), head[m.end():].strip()
        if company and role:
            return company, role, culture.strip()
        return None
    return None


class Calls:
    """Counts Claude calls and tokens per model, across threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.by_model = {}

    def add(self, model, usage):
        with self._lock:
            n = self.by_model.setdefault(model, {"calls": 0, "input": 0, "output": 0})
            n["calls"] += 1
            n["input"] += getattr(usage, "input_tokens", 0) or 0
            n["output"] += getattr(usage, "output_tokens", 0) or 0

    def total(self):
        return sum(n["calls"] for n in self.by_model.values())

    def cost(self):
        return sum(n["input"] / 1e6 * PRICES.get(m, (0, 0))[0]
                   + n["output"] / 1e6 * PRICES.get(m, (0, 0))[1]
                   for m, n in self.by_model.items())


def _call(calls, model, system, content, max_tokens):
    # A slow site must not hold the digest: 30 s a call, one SDK retry.
    reply, _ = llm.complete_json(model, system, [{"role": "user", "content": content}], max_tokens,
                                 purpose="company profile call", on_usage=lambda u: calls.add(model, u),
                                 timeout=30.0, max_retries=1)
    return reply


def summarise(calls, name, site):
    """The cached profile for a fetched site, or None when the pages are not
    this company's (a namesake's domain, a parked page)."""
    reply = _call(calls, config.PROFILE_MODEL, PROFILE_SYSTEM,
                  f"Company: {name}\n\nWebsite text:\n{site['text']}", 600)
    if reply.get("company_matches") is False:
        return None
    profile = {k: reply.get(k) for k in ("what", "who_for", "size", "mission")}
    profile["culture"] = [str(k).strip() for k in (reply.get("culture") or []) if str(k).strip()][:8]
    return profile


def rewrite_blurb(calls, job, profile):
    """The DEL-13 blurb with its company fragment and culture keywords
    rewritten from the profile, the role part copied unchanged, or None when
    the blurb cannot be split or the result breaks the word cap (the old
    blurb is kept)."""
    from scorer import format_rationale
    blurb, _ = split_rationale(job.get("rationale"))
    parts = split_blurb(blurb)
    if not parts:
        print(f"[Profile] blurb for {job.get('title', '')} at {job.get('company', '')} "
              "has no company / role / culture shape; kept as it is")
        return None
    company, role, culture = parts
    # "Culture:" is one word of the blurb; the rest is the role's.
    budget = min(18, BLURB_TARGET_WORDS - len(role.split()) - 1)
    if budget < 6:
        print(f"[Profile] role for {job.get('title', '')} at {job.get('company', '')} leaves "
              f"{budget} words for company and culture; blurb kept")
        return None
    product = {k: profile.get(k) for k in ("what", "who_for", "size", "mission")}
    content = (f"Title: {job.get('title', '')}\nCompany: {job.get('company', '')}\n\n"
               f"Current company fragment: {company}\n"
               f"Current culture keywords: {culture}\n"
               f"Word budget for company + culture together: {budget}\n\n"
               f"Website product facts: {json.dumps(product, ensure_ascii=False)}\n"
               f"Website culture keywords: {json.dumps(profile.get('culture') or [], ensure_ascii=False)}\n\n"
               f"Posting:\n{str(job.get('description') or '')[:6000]}")
    reply = _call(calls, BLURB_MODEL, REWRITE_SYSTEM, content, 300)
    new_culture = " ".join(str(reply.get("culture") or "").split())
    if new_culture.lower().startswith("culture:"):
        new_culture = new_culture[len("culture:"):].strip()
    new = format_rationale({"company": reply.get("company") or company, "role": role,
                            "culture": new_culture or NO_CULTURE})
    words = len(new.split())
    if words > config.BLURB_MAX_WORDS:
        print(f"[Profile] rewrite for {job.get('title', '')} at {job.get('company', '')} "
              f"ran {words} words; kept the posting-only blurb")
        return None
    return new


# -------------------------------------------------------------------- cache

def load_cache(path=CACHE_PATH):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"[Profile] cache unreadable ({type(e).__name__}: {e}); starting empty")
        return {}


def _fresh(entry, today):
    try:
        checked = date.fromisoformat(entry.get("checked", ""))
    except ValueError:
        return False
    if entry.get("status") == "ok" and entry.get("version") != PROFILE_VERSION:
        return False
    ttl = config.PROFILE_TTL_DAYS if entry.get("status") == "ok" else config.PROFILE_EMPTY_TTL_DAYS
    return today - checked < timedelta(days=ttl)


# --------------------------------------------------------------------- step

def enrich_blurbs(jobs, cache_path=CACHE_PATH, today=None,
                  budget_secs=None, write_cache=True, sites=None, http_factory=None):
    """Rewrite each job's blurb from its company's profile, in place.

    Returns a stats dict for the run log (and the dry run). Jobs whose company
    has no site, no readable site, or no profile keep their blurb."""
    start = time.monotonic()
    deadline = start + (budget_secs or config.PROFILE_BUDGET_SECS)
    today = today or date.today()
    calls = Calls()
    stats = {"jobs": len(jobs), "companies": 0, "no_employer": 0, "no_site": 0,
             "cache_hits": 0, "fetched": 0, "profiled": 0, "empty": 0, "errors": 0,
             "budget_skipped": 0, "rewritten": 0, "kept": 0, "calls": calls, "sites": {},
             "model_accepted": 0, "model_rejected": 0}
    if not jobs:
        return stats
    if not llm.available():
        print("[Profile] no API credentials; blurbs left as they are")
        return stats
    sites = sites or Sites()
    cache = load_cache(cache_path)
    http_factory = http_factory or (lambda: httpx.Client(headers=_headers(), follow_redirects=True))

    # One chain of candidate sites per company (DEL-15): the next is tried
    # only when one fails.
    by_company, job_company = {}, {}
    resolve_all = getattr(sites, "candidates", None) or (
        lambda *a: [r for r in [sites.resolve(*a)] if r[0]])
    for i, job in enumerate(jobs):
        name = profile_name(job)
        if not name:
            stats["no_employer"] += 1
            print(f"[Profile] {job.get('company', '')}: no named employer (unnamed staffing client); blurb kept")
            continue
        key = name.casefold()
        if key not in by_company:
            chain = resolve_all(name, job.get("apply_link", ""), job.get("website", ""))
            if not chain:
                stats["no_site"] += 1
                print(f"[Profile] {name}: no site found; blurb kept")
                continue
            by_company[key] = {"name": name, "chain": chain}
        job_company[i] = key
    stats["companies"] = len(by_company)

    def fetch_one(name, origin, method, deadline_at):
        """(entry, how) for one candidate site, fetched now."""
        try:
            with http_factory() as http:
                site = fetch_site(http, origin, deadline_at,
                                  verify_name=name if method == SCORING_CALL else "")
            entry = {"company": name, "method": method,
                     "checked": today.isoformat(), "version": PROFILE_VERSION,
                     "status": site["status"], "pages": site["pages"], "profile": None}
            if site["status"] == "ok":
                if time.monotonic() > deadline - 5:
                    return None, "budget"
                profile = summarise(calls, name, site)
                if profile is None:
                    entry["status"] = "not this company"
                entry["profile"] = profile
            return entry, "fetched"
        except llm.ApiAccessError:
            raise                                    # OPS-12: every call would fail; stop the step
        except Exception as e:                       # noqa: BLE001
            return {"status": f"error: {type(e).__name__}: {e}"[:160],
                    "pages": [], "profile": None}, "error"

    def profile_company(key):
        """[(domain, method, entry, how)] for every candidate this company
        reached, last one decisive. A fresh empty entry is held only when every
        source available now was tried when it was written ("tried"); an entry
        from before DEL-15 has no "tried" and is fetched again once."""
        info = by_company[key]
        name, chain = info["name"], info["chain"]
        available = {method for _, method in chain}
        attempts, tried = [], []
        company_deadline = None
        for origin, method in chain:
            domain = site_domain(origin)
            tried.append(method)
            entry = cache.get(domain)
            if entry and _fresh(entry, today):
                # A domain is one company's site. A hit built for another company
                # (a multi-tenant host the link classes do not know) is not used.
                if str(entry.get("company", "")).casefold() != key:
                    attempts.append((domain, method, {**entry, "status": "shared domain",
                                                      "profile": None}, "cache hit"))
                    continue
                if entry.get("status") == "ok":
                    attempts.append((domain, method, entry, "cache hit"))
                    return attempts
                held = entry.get("tried")
                if held is not None and available <= set(held):
                    attempts.append((domain, method, entry, "cache hit"))
                    return attempts
                if held is not None:
                    new = sorted(available - set(held))
                    print(f"[Profile] {name} ({domain}): held as {entry.get('status')}, but "
                          f"{', '.join(new)} not tried then; trying the next source")
                    attempts.append((domain, method, entry, "skipped hold"))
                    continue
                print(f"[Profile] {name} ({domain}): held as {entry.get('status')} before "
                      "sources were recorded; re-resolving once")
            if company_deadline is None:
                if time.monotonic() > deadline - config.PROFILE_COMPANY_BUDGET_SECS:
                    attempts.append((domain, method, None, "budget"))
                    return attempts
                company_deadline = min(time.monotonic() + config.PROFILE_COMPANY_BUDGET_SECS,
                                       deadline)
            elif time.monotonic() > company_deadline - 0.5:
                attempts.append((domain, method, None, "budget"))
                return attempts
            entry, how = fetch_one(name, origin, method, company_deadline)
            if how == "budget":
                attempts.append((domain, method, None, "budget"))
                return attempts
            entry["tried"] = list(tried)
            attempts.append((domain, method, entry, how))
            if entry.get("status") == "ok" and entry.get("profile"):
                return attempts
        # Every candidate reached and none profiled: each empty entry written
        # this run records the whole chain as tried, so the next digest holds.
        for _, _, entry, how in attempts:
            if how == "fetched":
                entry["tried"] = sorted(available)
        return attempts

    profiles, profile_domain, changed = {}, {}, False
    with ThreadPoolExecutor(max_workers=config.PROFILE_MAX_IN_FLIGHT) as pool:
        for key, attempts in zip(by_company, pool.map(profile_company, by_company)):
            name = by_company[key]["name"]
            for domain, method, entry, how in attempts:
                if how == "budget":
                    stats["budget_skipped"] += 1
                    print(f"[Profile] {name} ({domain}): step budget spent; blurb kept")
                    continue
                pages = entry.get("pages") or []
                chars = sum(p.get("chars", 0) for p in pages)
                stats["sites"][domain] = {"company": name, "method": method,
                                          "status": entry["status"], "how": how,
                                          "pages": len(pages), "chars": chars}
                if method == SCORING_CALL and how == "fetched":
                    if entry["status"] == NAME_NOT_ON_HOMEPAGE:
                        stats["model_rejected"] += 1
                        print(f"[Profile] {name}: scoring-call domain {domain} rejected, "
                              "homepage does not name the company; left unprofiled")
                    elif entry["status"] in ("ok", "not this company"):
                        stats["model_accepted"] += 1     # passed the name check
                if how in ("cache hit", "skipped hold"):
                    stats["cache_hits"] += how == "cache hit"
                elif how == "error":
                    stats["errors"] += 1
                else:
                    stats["fetched"] += 1
                if how == "fetched" and (entry["status"] == "ok" or entry["status"] in _EMPTY_STATUSES):
                    cache[domain] = entry
                    changed = True
                if entry.get("status") == "ok" and entry.get("profile"):
                    profiles[key] = entry["profile"]
                    profile_domain[key] = domain
                    stats["profiled"] += how != "cache hit"
                elif how not in ("error", "skipped hold"):
                    stats["empty"] += 1
                print(f"[Profile] {name} ({domain}, {method}): {how}, "
                      f"{entry['status']}, {len(pages)} pages, {chars:,} chars")

    if changed and write_cache:
        write_json_atomic(cache_path, dict(sorted(cache.items())), indent=1, ensure_ascii=False)

    def rewrite_one(i):
        job = jobs[i]
        if time.monotonic() > deadline - 3:
            return i, None, "budget"
        try:
            return i, rewrite_blurb(calls, job, profiles[job_company[i]]), "ok"
        except llm.ApiAccessError:
            raise
        except Exception as e:                       # noqa: BLE001
            return i, None, f"{type(e).__name__}: {e}"[:160]

    targets = [i for i, k in job_company.items() if k in profiles]
    with ThreadPoolExecutor(max_workers=config.PROFILE_MAX_IN_FLIGHT) as pool:
        for i, new, how in pool.map(rewrite_one, targets):
            job = jobs[i]
            if new:
                job["rationale"] = join_rationale(new, split_rationale(job.get("rationale"))[1])
                job["profile_domain"] = profile_domain[job_company[i]]
                stats["rewritten"] += 1
            else:
                stats["kept"] += 1
                if how != "ok":
                    print(f"[Profile] rewrite for {job.get('title', '')} at "
                          f"{job.get('company', '')} failed ({how}); blurb kept")

    stats["elapsed"] = time.monotonic() - start
    print(f"[Profile] {stats['jobs']} roles, {stats['companies']} company sites "
          f"({stats['cache_hits']} cached, {stats['fetched']} fetched, {stats['errors']} errors, "
          f"{stats['budget_skipped']} over budget; {stats['no_site']} no site, "
          f"{stats['no_employer']} unnamed employer; scoring-call domains {stats['model_accepted']} "
          f"accepted, {stats['model_rejected']} rejected); {stats['rewritten']} blurbs rewritten; "
          f"{calls.total()} Claude calls, ~${calls.cost():.3f}; {stats['elapsed']:.0f}s")
    return stats
