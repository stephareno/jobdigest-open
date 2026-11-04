# websites.py — the website ladder: a company's own website from its name (PUB-12)
#
# resolve needs a company's website to find its careers page; PUB-11 found 37%
# of the public list had none. The ladder tries, in order, and trusts a result
# only once the page it serves names the company (the resolver's content check,
# tools/resolve_ats.page_confirms_company, on the cleaned name):
#
#   1. list      the list's own website column
#   2. wikidata  Wikidata's official website (P856) for items found by name
#                (wbsearchentities, then wbgetentities; replies cached on disk)
#   3. guess     domain guesses from the name (domain_guesses: at most six, a
#                HEAD each, a GET only for a host that answers 2xx)
#   4. model     only when asked (--guess-websites): one call to the utility
#                model (config.PROFILE_MODEL) for the official website, given
#                the name, state and SIC description. Every guess that fails the
#                check is logged.
#
# The first verified rung wins; its name goes in the website_source column
# (list | wikidata | guess | model). Network use is read-only. Standalone
# runnable pieces take their HTTP and model calls as arguments, so the tests
# stub them.

import hashlib
import html as html_lib
import json
import os
import re
import threading
import time
from urllib.parse import urlsplit

SOURCES = ("list", "wikidata", "guess", "model")
# Ladder.find's source when a guessed domain failed the stricter check: the
# company is recorded as nothing found, with the note, not resolved on.
GUESS_REJECTED = "guess_rejected"
GUESS_REJECTED_NOTE = "guessed domain, name not on page"
MAX_GUESSES = 6
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_INTERVAL = 0.2          # seconds between Wikidata API calls (5 a second)
WIKIDATA_CANDIDATES = 3          # search hits looked at per name
PAGE_TIMEOUT = 10
MAX_PAGE_BYTES = 2_000_000

# Words dropped from the end of a name before guessing a domain or checking a
# page: legal forms, and the SEC's state-of-incorporation tag ("/DE/").
LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited",
    "llc", "lp", "llp", "plc", "holdings", "holding", "group", "the", "nv", "sa", "ag", "se",
}
_SEC_STATE_TAG = re.compile(r"\s*/[A-Z]{2,4}/?\s*$")         # "/DE/", "/NEW/", "/ADR"
# Legal words a company keeps in its domain often enough to guess (acme-group.com);
# "inc" or "corp" in a domain is the <slug>corp.com guess's job.
DOMAIN_WORDS = {"group", "holdings", "holding", "company", "companies"}

MODEL_SYSTEM = ("You name a company's official website. Reply with JSON only: "
                '{"website": "<the homepage URL, or null if you do not know it>"}. '
                "Never guess a domain you are not confident belongs to this exact company.")


# --- Names ----------------------------------------------------------------------------------

def clean_name(name):
    """The name as a person would say it: the SEC state tag and trailing legal
    forms dropped, '&' read as 'and'. "NVIDIA CORP /DE/" -> "NVIDIA"."""
    name = _SEC_STATE_TAG.sub("", name or "").strip()
    words = re.sub(r"[^\w&'.-]+", " ", name).split()
    while len(words) > 1 and (re.sub(r"[^a-z]", "", words[-1].lower()) in LEGAL_SUFFIXES | {"and", ""}):
        words = words[:-1]
    return " ".join(words).strip(" ,.-&")


def _slug_words(name):
    """(kept words, the legal word removed or "") as lowercase alphanumerics."""
    name = _SEC_STATE_TAG.sub("", name or "").replace("&", " and ")
    words = [re.sub(r"[^a-z0-9]", "", w) for w in re.split(r"[\s,/.-]+", name.lower())]
    words = [w for w in words if w]
    removed = ""
    while len(words) > 1 and words[-1] in LEGAL_SUFFIXES:
        if words[-1] in DOMAIN_WORDS and not removed:
            removed = words[-1]
        words = words[:-1]
    if words and words[0] == "the" and len(words) > 1:
        words = words[1:]
    return words, removed


def domain_guesses(name):
    """At most six https://<host> guesses, most likely first: <slug>.com,
    <hyphenated>.com, <slug>corp.com, <slug>-<removed legal word>.com,
    <first word>.com and <initials>.com, each only when it differs from those
    before it. Punctuation and legal suffixes (Inc, Corp, Ltd, Holdings, Group,
    Co, ...) are stripped first."""
    words, removed = _slug_words(name)
    if not words:
        return []
    slug = "".join(words)
    hosts = [f"{slug}.com"]
    if len(words) > 1:
        hosts.append(f"{'-'.join(words)}.com")
    hosts.append(f"{slug}corp.com")
    if removed:
        hosts.append(f"{slug}-{removed}.com")
    if len(words) > 1 and len(words[0]) > 3:
        hosts.append(f"{words[0]}.com")
    if len(words) > 1:
        hosts.append(f"{''.join(w[0] for w in words)}.com")
    return [f"https://{h}" for h in dict.fromkeys(hosts)][:MAX_GUESSES]


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.netloc or "." not in parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


# --- HTTP -------------------------------------------------------------------------------------

class Http:
    """requests-based HEAD/GET with a descriptive User-Agent. Each method
    returns (status, final_url, text); status 0 on a network error."""

    def __init__(self, user_agent, timeout=PAGE_TIMEOUT):
        import requests
        self._local = threading.local()
        self._requests = requests
        self.user_agent, self.timeout = user_agent, timeout

    def _session(self):
        if not hasattr(self._local, "s"):
            s = self._requests.Session()
            s.headers.update({"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.8"})
            self._local.s = s
        return self._local.s

    def head(self, url):
        try:
            r = self._session().head(url, timeout=self.timeout, allow_redirects=True)
            return r.status_code, r.url, ""
        except Exception:                                       # noqa: BLE001 -- a miss, never a crash
            return 0, url, ""

    def get(self, url):
        try:
            r = self._session().get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            body = r.raw.read(MAX_PAGE_BYTES, decode_content=True) or b""
            text = body.decode(r.encoding or "utf-8", errors="replace")
            return r.status_code, r.url, text
        except Exception:                                       # noqa: BLE001
            return 0, url, ""

    def json(self, url, params):
        r = self._session().get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_SITE_NAME = re.compile(r"""<meta[^>]+(?:property|name)=["']og:site_name["'][^>]*content=["']([^"']*)"""
                           r"""|<meta[^>]+content=["']([^"']*)["'][^>]*(?:property|name)=["']og:site_name""", re.I)
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def _norm(text):
    text = (text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def host_label(url):
    """The registrable label of a URL's host: www.amgen.com -> amgen."""
    from tools.resolve_ats import throttle_key
    return throttle_key(urlsplit(url).hostname or "").split(".")[0]


def page_names_company(html, name, url=""):
    """The resolver's content check (the page's own text names the company),
    tightened for a guessed site: the name must be in the page title, or the
    host must match the name and the name be anywhere on the page. A name
    matches with or without its spaces ("JPMorganChase"); a host made of the
    name's initials (amd.com) matches a title that names those initials."""
    from tools.resolve_ats import name_phrase
    phrase = name_phrase(clean_name(name))
    if not phrase:
        return False
    compact = phrase.replace(" ", "")
    body = _SCRIPT.sub(" ", html or "")
    title = _norm(" ".join(_TITLE.findall(body)))
    text = _norm(re.sub(r"<[^>]+>", " ", body)[:200_000])

    def names(t):
        return phrase in t or (len(compact) >= 6 and compact in t.replace(" ", ""))
    if names(title):
        return True
    label = host_label(url) if url else ""
    words = phrase.split()
    initials = "".join(w[0] for w in words) if len(words) >= 3 else ""
    if initials and label == initials and initials in title.split() and names(text):
        return True                                              # amd.com: "AMD" titled, the full name on the page
    return _host_matches(url, name) and names(text)


def distinctive_token(name):
    """The company's distinctive name token, casefolded: the first word after the
    legal suffixes are stripped, punctuation dropped, if it has 4+ letters; else ""."""
    words = [w for w in clean_name(name).split() if w.casefold() != "the"]
    word = re.sub(r"[^a-z0-9]", "", words[0].casefold()) if words else ""
    return word if len(re.sub(r"[^a-z]", "", word)) >= 4 else ""


def title_names_company(html, name, url=""):
    """PUB-13's stricter check for a guessed domain: the page's <title>, its
    og:site_name or (PUB-13b) the registrable domain holds the distinctive name
    token. A name with no such token (3M, BJ's) cannot be checked and passes."""
    token = distinctive_token(name)
    if not token or token in re.sub(r"[^a-z0-9]", "", host_label(url) if url else ""):
        return True
    body = _SCRIPT.sub(" ", html or "")
    names = _TITLE.findall(body) + [a or b for a, b in _OG_SITE_NAME.findall(body)]
    return token in _norm(html_lib.unescape(" ".join(names))).replace(" ", "")


def _host_matches(url, name):
    """The host's registrable label is the name, part of it, or starts from its first word."""
    from tools.resolve_ats import name_phrase
    phrase = name_phrase(clean_name(name))
    label = host_label(url) if url else ""
    if not phrase or not label:
        return False
    compact, first = phrase.replace(" ", ""), phrase.split()[0]
    return label in compact or compact in label or (len(first) >= 4 and first in label)


# Candidate pages that refused to be read (401/403/429/503: a bot wall), so the
# check could not run; counted for the build report.
BLOCKED = {"n": 0}
_BLOCKED_LOCK = threading.Lock()


# A parked or for-sale domain names whatever was typed into it.
_PARKED = re.compile(r"(is for sale|buy this domain|domain (name )?(is )?for sale|hugedomains|sedo\.com|dan\.com|"
                     r"afternic|parkingcrew|this domain (may be|is) for sale)", re.I)


def _root(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}"


def verify(http, url, name, head_first=False):
    """The homepage URL (after redirects, path dropped) when the homepage of
    `url` serves a page that names the company, else None. The homepage is what
    is kept, so the homepage is what is checked (a journal page on sciencedirect
    that names "Applied Materials" is not Applied Materials' site). A for-sale
    page, or a redirect to a host that does not match the name, fails.
    head_first: a HEAD must answer 2xx (or 405, a server that refuses HEAD)
    before the GET."""
    url = normalize_url(url)
    if not url:
        return None
    url = _root(url)
    if head_first:
        status, _, _ = http.head(url)
        if not (200 <= status < 300 or status == 405):
            return None
    status, final, html = http.get(url)
    if status in (401, 403, 429, 503):
        with _BLOCKED_LOCK:
            BLOCKED["n"] += 1
    if not (200 <= status < 300) or _PARKED.search(html[:50_000] or ""):
        return None
    final = final or url
    if host_label(final) != host_label(url) and not _host_matches(final, name):
        return None                                              # redirected to someone else's site
    if not page_names_company(html, name, final):
        return None
    return _root(final)


# --- Wikidata by name -------------------------------------------------------------------------

class Wikidata:
    """Official websites (P856) of the items Wikidata's search returns for a
    name, best match first. Every API reply is cached in `cache_dir`, keyed by
    its parameters, so a re-run asks nothing it asked before."""

    def __init__(self, get_json, cache_dir, sleep=time.sleep, clock=time.monotonic):
        self.get_json, self.cache_dir = get_json, cache_dir
        self.sleep, self.clock = sleep, clock
        self._lock = threading.Lock()
        self._last = 0.0
        self.fetched = self.cached = 0

    def _call(self, params):
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        path = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(path):
            with open(path) as fh:
                self.cached += 1
                return json.load(fh)
        with self._lock:                                         # one call at a time, throttled
            delay = WIKIDATA_INTERVAL - (self.clock() - self._last)
            if delay > 0:
                self.sleep(delay)
            data = self.get_json(WIKIDATA_API, {**params, "format": "json", "maxlag": 5})
            self._last = self.clock()
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh)
        self.fetched += 1
        return data

    def websites(self, name):
        query = clean_name(name)
        if not query:
            return []
        hits = self._call({"action": "wbsearchentities", "search": query, "language": "en",
                           "type": "item", "limit": WIKIDATA_CANDIDATES}).get("search") or []
        ids = [h["id"] for h in hits if h.get("id")]
        if not ids:
            return []
        entities = self._call({"action": "wbgetentities", "ids": "|".join(ids),
                               "props": "claims"}).get("entities") or {}
        out = []
        for qid in ids:
            claims = (entities.get(qid) or {}).get("claims") or {}
            sites = [c["mainsnak"]["datavalue"]["value"] for c in claims.get("P856") or []
                     if (c.get("mainsnak") or {}).get("datavalue")]
            # a .com over a country site, then the bare homepage over a subpage
            out += sorted(sites, key=lambda u: (not (urlsplit(u).hostname or "").endswith(".com"), len(u)))[:1]
        return list(dict.fromkeys(out))


# --- The model rung ---------------------------------------------------------------------------

class ModelGuesser:
    """One utility-model call per company: its official website, given name,
    state and SIC description. Counts calls and tokens for the cost line."""

    def __init__(self, model, cache_dir=None):
        self.model, self.cache_dir = model, cache_dir
        self._lock = threading.Lock()
        self.calls = self.input_tokens = self.output_tokens = self.cached = 0

    def guess(self, name, state="", sic_description=""):
        """The model's answer, from cache_dir when this exact question was asked before."""
        path = None
        if self.cache_dir:
            key = hashlib.sha256(json.dumps([self.model, name, state, sic_description]).encode()).hexdigest()[:24]
            path = os.path.join(self.cache_dir, f"{key}.json")
            if os.path.exists(path):
                with open(path) as fh, self._lock:
                    self.cached += 1
                    return json.load(fh)["website"]
        site = self._ask(name, state, sic_description)
        if path:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w") as fh:
                json.dump({"name": name, "website": site}, fh)
        return site

    def _ask(self, name, state, sic_description):
        facts = [f"Company: {name}"]
        if state:
            facts.append(f"Headquarters state: {state}")
        if sic_description:
            facts.append(f"Industry (SIC): {sic_description}")
        import llm
        text, usage = llm.complete(self.model, MODEL_SYSTEM, [{"role": "user", "content": "\n".join(facts)}], 100,
                                   json_mode=True, purpose="website guess call")
        with self._lock:
            self.calls += 1
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return normalize_url((json.loads(m.group(0)) if m else {}).get("website") or "")
        except ValueError:
            return ""

    def cost(self):
        import config
        return config.model_cost(self.model, self.input_tokens, self.output_tokens)


# --- The ladder -------------------------------------------------------------------------------

class Ladder:
    """find(row) -> (website, source), ("", "") when no rung verifies.

    row: a list row (name, website, and state / sic_description when known).
    wikidata: a Wikidata (or None to skip that rung). guesser: a ModelGuesser,
    or None (the default: --guess-websites off)."""

    def __init__(self, http, wikidata=None, guesser=None, log=print):
        self.http, self.wikidata, self.guesser, self.log = http, wikidata, guesser, log
        self.rejected = []                                       # (name, source, url) that failed the check

    def _try(self, name, source, url, head_first=False):
        site = verify(self.http, url, name, head_first=head_first)
        if site is None and url:
            self.rejected.append((name, source, url))
            if source == "model":
                self.log(f"[Websites] model guess rejected: {name} -> {url} (page does not name the company)")
        return site

    def find(self, row):
        name = row["name"]
        if row.get("website"):
            site = self._try(name, "list", row["website"])
            if site:
                return site, "list"
        if self.wikidata is not None:
            try:
                candidates = self.wikidata.websites(name)
            except Exception as e:                                # noqa: BLE001 -- the next rung still runs
                self.log(f"[Websites] Wikidata lookup failed for {name}: {type(e).__name__}: {e}")
                candidates = []
            for url in candidates:
                site = self._try(name, "wikidata", url)
                if site:
                    return site, "wikidata"
        for url in domain_guesses(name):
            site = verify(self.http, url, name, head_first=True)
            if site:
                if title_names_company(self.http.get(site)[2], name, site):
                    return site, "guess"
                self.log(f"[Websites] guessed domain rejected: {name} -> {site} ({GUESS_REJECTED_NOTE})")
                return "", GUESS_REJECTED
        if self.guesser is not None:
            url = self.guesser.guess(name, row.get("state", ""), row.get("sic_description", ""))
            if url:
                site = self._try(name, "model", url)
                if site:
                    return site, "model"
            else:
                self.log(f"[Websites] model guess: no website named for {name}")
        return "", ""


def run_ladder(rows, ladder, workers=16, log=print, every=250):
    """{row index: (website, source)} for every row, `workers` at a time."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (i, result) in enumerate(zip(range(len(rows)), pool.map(ladder.find, rows)), 1):
            out[i] = result
            if n % every == 0:
                log(f"[Websites] {n}/{len(rows)} companies through the ladder")
    return out
