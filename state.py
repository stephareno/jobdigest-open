# state.py — tracks seen job URLs to prevent duplicate sends,
# and builds the two dedup keys a posting is matched on (FIL-10).

import json
import os
import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

STATE_FILE = "seen_jobs.json"
SKIP_STATE_FILE = "skip_state.json"
RETENTION_DAYS = 60

# Query parameters that are tracking-only and should be stripped before dedup
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "ref", "source", "src", "referrer", "referer", "sid", "cid",
    "mid", "tracking", "track", "affiliate", "clickid", "click_id", "fbclid",
    "gclid", "msclkid", "dclid", "zanpid", "origin", "mcid", "trk", "trkInfo",
}


def normalize_url(url):
    """Strip tracking parameters and fragment from a URL for stable deduplication."""
    url = (url or "").strip()
    if not url:
        return url
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(filtered, doseq=True)
        clean = parsed._replace(query=clean_query, fragment="")
        return urlunparse(clean).rstrip("/")
    except Exception:
        return url.rstrip("/")


# --- FIL-10 cross-source dedup key --------------------------------------------
#
# The URL key (job_hash) stays the first key: same tracking-stripped URL, same
# job. The second key catches the same opening reaching us from two sources
# under two URLs (Dragos VP IT: four rows from Adzuna, JSearch, LinkedIn and
# ATS). It is normalised company + normalised title + normalised location.
#
# Every rule here is deliberately conservative: a merge that should not happen
# loses a real opening, a merge that is missed only costs a duplicate row.
# Normalisation only removes noise the same posting picks up in transit
# (legal suffixes, "(Remote)" in a title, "US" on the end of a location) and
# expands the abbreviations sources genuinely disagree on ("VP" vs "Vice
# President", "IT" vs "Information Technology"). Nothing here reorders or
# drops meaningful words.

# Dropped from a company name wherever they appear: legal form only, never
# words that distinguish two companies ("Group", "Technologies", "Labs").
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "ltd", "limited", "corp",
    "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "nv", "bv",
    "pty", "llp", "lp", "pbc", "sas", "oy", "ab", "as",
}

# Title abbreviations sources spell differently for the same role.
_TITLE_EXPANSIONS = {
    "vp": "vice president",
    "svp": "senior vice president",
    "evp": "executive vice president",
    "avp": "associate vice president",
    "sr": "senior",
    "jr": "junior",
    "dir": "director",
    "mgr": "manager",
    "exec": "executive",
    "it": "information technology",
    "ot": "operational technology",
    "hr": "human resources",
    "ops": "operations",
    "infosec": "information security",
    "sec": "security",
    "eng": "engineering",
    "svcs": "services",
    "tech": "technology",
    "info": "information",
    "grp": "group",
    "natl": "national",
    "&": "and",
}

# Dropped from a title: connectives that carry no role information.
_TITLE_FILLER = {"of", "the", "for", "a", "an", "and", "to", "at", "in", "on"}

# A bracketed aside is dropped only when it holds no digits: "(Remote)" and
# "(IT)" are noise, but MetroStar's "(6821)" and "(6820)" are the only thing
# telling two of its openings apart, so any number in a title is kept.
_BRACKETED_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")


def _words(text):
    """Lowercase word tokens, punctuation to spaces, '&' kept as a word."""
    text = (text or "").lower().replace("&", " & ").replace("’", "'")
    text = re.sub(r"[^a-z0-9'& ]+", " ", text)
    return [w for w in text.replace("'", "").split() if w]


def company_key(company):
    """Normalised company name: lowercase words, legal form dropped."""
    words = [w for w in _words(company) if w not in _COMPANY_SUFFIXES]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _collapse_repeats(words):
    """Drop an immediately repeated token or bigram, which expansion creates:
    'IT (Information Technology)' -> 'information technology information
    technology' -> 'information technology'."""
    out = []
    for w in words:
        out.append(w)
        if len(out) >= 2 and out[-1] == out[-2]:
            out.pop()
        elif len(out) >= 4 and out[-2:] == out[-4:-2]:
            del out[-2:]
    return out


def title_key(title):
    """Normalised job title: digit-free bracketed asides removed, abbreviations
    expanded, connectives dropped. Word order and any number are preserved."""
    text = _BRACKETED_RE.sub(
        lambda m: m.group(1) if any(c.isdigit() for c in m.group(1)) else " ",
        (title or "").lower())
    words = []
    for w in _words(text):
        words.extend(_TITLE_EXPANSIONS.get(w, w).split())
    words = [w for w in words if w not in _TITLE_FILLER]
    return " ".join(_collapse_repeats(words))


# Location is normalised by resolving it with location.py and keying on the
# place it resolved to, so "Hanover, MD", "Hanover, Maryland, US" and Adzuna's
# "Hanover, Anne Arundel County" all land on the same GeoNames row and give the
# same key, while Baltimore and Rockville stay apart.
#
# LOCATION_LEVEL "place" keys on that place. "metro" keys instead on the
# nearest city of METRO_MIN_POPULATION+ within METRO_RADIUS_MILES, which also
# merges a role posted as "Arlington, VA" on one board and "Washington, DC" on
# another — at the cost of merging two genuinely different offices in one metro.
LOCATION_LEVEL = "place"
METRO_MIN_POPULATION = 500_000
METRO_RADIUS_MILES = 60

_LOCATION_NOISE = {"us", "usa", "u s", "united states", "united states of america",
                   "america", "remote", "hybrid", "onsite", "on site", "in office",
                   "in person", "anywhere", "nationwide", "flexible", "various",
                   "multiple", "multiple locations", "hq", "headquarters", "office",
                   "work from home", "wfh", "virtual", "distributed", "north america"}

# Separators between two alternative locations ("Austin, TX; Denver, CO"),
# as opposed to the comma, which separates parts of one location.
_LOCATION_ALT_RE = re.compile(r"[/|;\n]|\s[-–]\s")


def _metro_index():
    """[(name, state, lat, lon)] for every US place of METRO_MIN_POPULATION+.

    Built once from location.py's already-loaded places file (~35 cities at
    this threshold). location is imported lazily so tools that only need the
    URL state never pay for the places file.
    """
    import location
    index = getattr(location, "_fil10_metros", None)
    if index is None:
        index = [(p.name, p.state, p.lat, p.lon)
                 for plist in location._places()[0].values()
                 for p in plist if p.population >= METRO_MIN_POPULATION]
        location._fil10_metros = index
    return index


def _metro_of(point):
    import location as loc_mod
    near = [(loc_mod.miles_between(point, (lat, lon)), name, state)
            for name, state, lat, lon in _metro_index()]
    near = [n for n in near if n[0] <= METRO_RADIUS_MILES]
    if not near:
        return None
    _, name, state = min(near)
    return f"{company_key(name)}|{state}"


def location_key(loc, level=None):
    """Normalised location, or '' when the string names no place.

    Blank, "Remote", "United States" and the like all normalise to '' — a
    remote posting names no place, so two remote copies of one role match on
    company + title alone. A string that resolves to US points takes those
    points (or their metros); a state-only string takes the state; anything
    else keeps its cleaned text, so two locations we cannot place are only ever
    equal to each other, never to a placed one.
    """
    import location as loc_mod

    level = level or LOCATION_LEVEL
    text = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", (loc or "").lower())
    text = re.sub(r"[\(\)\[\]\{\}]", ",", text)
    alternatives = []
    for alt in _LOCATION_ALT_RE.split(text):
        parts = []
        for part in alt.split(","):
            cleaned = " ".join(_words(part))
            if cleaned and cleaned not in _LOCATION_NOISE:
                parts.append(part.strip())
        if parts:
            alternatives.append(", ".join(parts))
    if not alternatives:
        return ""

    cleaned_text = "; ".join(alternatives)
    # resolve_location first: "New York, NY" is a place, not the state of New York.
    points = loc_mod.resolve_location(cleaned_text, most_populous=True)
    if points:
        if level == "metro":
            keys = sorted({_metro_of(p) or f"{p[0]:.4f},{p[1]:.4f}" for p in points})
        else:
            keys = sorted({f"{lat:.4f},{lon:.4f}" for lat, lon in points})
        return f"{level}:" + "+".join(keys)
    abbr = loc_mod.state_only(cleaned_text)
    if abbr:
        return f"state:{abbr}"
    return "text:" + " ".join(_words(cleaned_text))


def content_key(job, level=None):
    """The FIL-10 second dedup key for a posting, or None when it has no
    company or no title to key on (those fall back to the URL key alone).

    Keys on the posted name, as the fetch run sees it before scoring: a scored
    job's posted_company, or the via name in a Sheet row's Company (FIL-13)."""
    company = company_key(job.get("posted_company") or poster_name(job.get("company")))
    title = title_key(job.get("title"))
    if not company or not title:
        return None
    return (company, title, location_key(job.get("location"), level=level))


# --- Employer and poster (FIL-13) --------------------------------------------
#
# Company holds the employer the role works for. When a staffing firm,
# recruiter or job board posted the role and the scoring call named the
# employer (hiring_company, SCO-06), Company reads "<employer> (via <poster>)";
# otherwise it holds the posted name. Anything that matches on company reads
# it through employer_name / company_names, never as raw text.

# The poster may carry its own brackets: "Grant Thornton (US)".
_VIA_RE = re.compile(r"^(?P<employer>.*?\S)\s+\(via (?P<poster>.+)\)\s*$")

# What a scoring call sometimes returns in place of a name. hiring_company is
# a proper name per SCO-06; these are refused rather than written to Company.
_NOT_A_NAME = {"confidential", "undisclosed", "client", "our client", "a client",
               "unknown", "n a", "na", "none", "null", "stealth", "stealth startup",
               "not disclosed", "confidential client", "undisclosed client"}
MAX_EMPLOYER_NAME_WORDS = 12  # VA's "Department of Veterans Affairs, Office of Information and Technology" is 9


def employer_name(company):
    """The employer in a Company value: the " (via …)" tail removed."""
    company = (company or "").strip()
    m = _VIA_RE.match(company)
    return m.group("employer").strip() if m else company


def poster_name(company):
    """Who posted the role: the via name when there is one, else Company itself."""
    company = (company or "").strip()
    m = _VIA_RE.match(company)
    return m.group("poster").strip() if m else company


def company_names(company):
    """Every name a Company value answers to: employer first, then the poster."""
    names = [employer_name(company), poster_name(company)]
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def _proper_name(name):
    """True when a scoring call's hiring_company reads as a name, not a description."""
    words = _words(name)
    return (bool(words) and len(words) <= MAX_EMPLOYER_NAME_WORDS
            and " ".join(words) not in _NOT_A_NAME
            and (name.strip()[0].isupper() or name.strip()[0].isdigit()))


def _same_employer(a, b):
    """Two names for one employer: equal once legal form and bracketed asides
    are dropped, or one is the other's leading words ("Grant Thornton" and
    "Grant Thornton (US)", "World Food Program" and "World Food Program USA")."""
    ka = company_key(_BRACKETED_RE.sub(" ", a or "")).split()
    kb = company_key(_BRACKETED_RE.sub(" ", b or "")).split()
    if not ka or not kb:
        return False
    shorter, longer = sorted((ka, kb), key=len)
    return longer[:len(shorter)] == shorter


def name_employer(job):
    """Set Company to "<hiring_company> (via <posted company>)" after scoring.

    Only when hiring_company is a proper name that differs from the posted
    company; otherwise Company stays as posted. The posted name is kept in
    posted_company, which the FIL-10 content key reads. Idempotent: a job
    already named is left alone. Returns True when Company changed.
    """
    posted = (job.get("company") or "").strip()
    if "posted_company" in job or _VIA_RE.match(posted):
        return False
    job["posted_company"] = posted
    hiring = " ".join((job.get("hiring_company") or "").split())
    if not hiring or _same_employer(hiring, posted):
        return False
    if not _proper_name(hiring):
        print(f"[Employer] not a name, Company left as posted: hiring_company={hiring!r} "
              f"for {job.get('title', '')!r} at {posted!r}")
        return False
    job["company"] = f"{hiring} (via {posted})" if posted else hiring
    return True


def hiring_key(job):
    """The FIL-13 digest-time key: normalised employer + title, no location.

    The employer is Company without its via tail (name_employer), so a role a
    staffing firm posted for a named client keys on the client, and one with
    no named client keys on the staffing firm. None when there is no company or
    title to key on.
    """
    company = company_key(employer_name(job.get("company")))
    title = title_key(job.get("title"))
    if not company or not title:
        return None
    return (company, title)


# --- The same-board guard ------------------------------------------------------
#
# Two rows on the SAME employer board under DIFFERENT posting ids are two
# requisitions, however alike their company, title and location read: MITRE's
# R117090 and R116664 are both "Lead Systems Engineer" in McLean. Rows on
# different hosts say nothing about each other, so they still merge — that is
# the whole point of the key (Dragos on Greenhouse, LinkedIn and Adzuna is one
# job).
#
# FIL-13: the guard applies to employer boards only — ATS vendor hosts, company
# domains and usajobs.gov, where a posting id is a requisition. On LinkedIn, the
# job boards and the aggregators a posting id is a listing, and a staffing firm
# reposts one opening under a fresh id every few days (CyberCoders "VP of IT,
# Worcester" ran three ids in five days), so there two postings with the same
# content key are one job however many ids they carry.

# usajobs.gov is classed a job board for apply-link priority, but every posting
# on it is the agency's own announcement, so its ids are requisitions.
_EMPLOYER_BOARD_HOSTS = ("usajobs.gov",)


def employer_board(host):
    """True when a posting id on this host identifies a requisition (FIL-13)."""
    from url_resolver import link_class  # url_resolver imports state
    host = (host or "").lower()
    if any(host == h or host.endswith("." + h) for h in _EMPLOYER_BOARD_HOSTS):
        return True
    return link_class(f"https://{host}/") in ("ats", "company")

_BOARD_ID_RE = re.compile(
    r"(?:^|[-_])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z]{0,2}\d{4,})$",
    re.I)


def board_id(url):
    """(host, posting id) for an apply link, or None when no id is visible.

    The id is the last path segment that ends in a long number or a UUID, so
    a slug in front of it does not matter: LinkedIn's /jobs/view/4457489553,
    JSearch's /jobs/view/director-it-at-figma-4397204084 and Greenhouse's
    /dragos/jobs/5399442008 all give one, and MITRE's
    /us/en/job/R117090/Lead-Systems-Engineer gives R117090.
    """
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    for segment in reversed([s for s in parsed.path.split("/") if s]):
        m = _BOARD_ID_RE.search(segment)
        if m:
            return (parsed.netloc.lower(), m.group(1).lower())
    return None


def same_posting(idents, url):
    """True when a posting at `url` is the same one already recorded under a
    content key, given the board idents recorded for it.

    False only when the guard fires: the link is on an employer board and one
    of the recorded idents is on the same host with a different posting id. A
    link with no visible id cannot prove it is a different requisition, and an
    id on LinkedIn or an aggregator is only a listing (FIL-13), so both count
    as the same posting.
    """
    ident = board_id(url)
    if ident is None or not employer_board(ident[0]):
        return True
    return not any(host == ident[0] and posting != ident[1] for host, posting in idents)


def _load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def write_json_atomic(path, data, **dump_kwargs):
    """Write JSON via a temp file and rename, so a reader never sees half a file
    and a run killed mid-write leaves the previous version intact."""
    path = str(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, **dump_kwargs)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _save(state):
    write_json_atomic(STATE_FILE, state, indent=2)


def job_hash(url):
    return hashlib.md5(normalize_url(url).lower().encode()).hexdigest()


def is_seen(url):
    state = _load()
    return job_hash(url) in state


def load_url_index(state=None):
    """Every seen-URL hash in one set, so a run reads the file once."""
    state = _load() if state is None else state
    return {k for k in state if not k.startswith((CONTENT_PREFIX, DEFER_PREFIX))}


def mark_seen(url):
    state = _load()
    state[job_hash(url)] = datetime.utcnow().isoformat()
    _save(state)


def mark_seen_batch(urls):
    state = _load()
    for url in urls:
        state[job_hash(url)] = datetime.utcnow().isoformat()
    _save(state)


# --- Persisted content keys (FIL-10) -------------------------------------------
#
# seen_jobs.json holds both keys in one flat dict, so an old file loads
# unchanged and no URL hash ever moves:
#
#   "<32 hex>"        : "<iso>"                       a seen URL (FIL-03)
#   "c:<32 hex>"      : {"ts": "<iso>",               a seen content key
#                        "boards": ["host|id", ...]}  the postings behind it
#
# The boards list is what the same-board guard is checked against on the next
# run. Both kinds age out together in purge_old_entries. A posting dropped as a
# repeat of a persisted key re-stamps that key's ts (FIL-12), so a staffing
# firm reposting one opening every few days never outlives its key.
#
# Written only by fetch runs, like every other key here (rule 11).

CONTENT_PREFIX = "c:"
MAX_BOARDS_PER_KEY = 20  # a content key in practice has one or two


def content_hash(key):
    """Storage key for a content key tuple from content_key()."""
    return CONTENT_PREFIX + hashlib.md5("\x1f".join(key).encode()).hexdigest()


def _entry_ts(entry):
    """The ISO timestamp of either entry shape, or None if it has none."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        ts = entry.get("ts")
        return ts if isinstance(ts, str) else None
    return None


def _entry_boards(entry):
    """The board idents recorded on a content entry, as (host, id) tuples."""
    if not isinstance(entry, dict):
        return []
    out = []
    for raw in entry.get("boards") or []:
        host, _, posting = str(raw).partition("|")
        if host and posting:
            out.append((host, posting))
    return out


def load_content_index(state=None):
    """{storage key: [(host, id), ...]} for every persisted content key.

    Read once at the start of a fetch run: the run checks against this in
    memory rather than re-reading the file for every posting.
    """
    state = _load() if state is None else state
    return {k: _entry_boards(v) for k, v in state.items() if k.startswith(CONTENT_PREFIX)}


def mark_content_seen_batch(entries, when=None):
    """Record content keys as seen. entries: [(content key, [urls])].

    Every url the posting was known by contributes a board ident — both the
    aggregator link it arrived as and the direct link it resolved to — so a
    later copy on either host is matched against the right posting id.
    """
    if not entries:
        return 0
    state = _load()
    stamp = (when or datetime.utcnow()).isoformat()
    for key, urls in entries:
        if not key:
            continue
        storage = content_hash(key)
        boards = [f"{host}|{posting}" for host, posting in _entry_boards(state.get(storage))]
        for url in urls:
            ident = board_id(url)
            if ident:
                flat = f"{ident[0]}|{ident[1]}"
                if flat not in boards:
                    boards.append(flat)
        state[storage] = {"ts": stamp, "boards": boards[-MAX_BOARDS_PER_KEY:]}
    _save(state)
    return len(entries)


# --- Scoring deferrals (OPS-12) ------------------------------------------------
#
#   "d:<32 hex>" : {"ts": "<iso>", "count": n}
#
# A posting whose scoring call failed on a transient API error is neither
# queued nor marked seen, so the next fetch run that serves it scores it
# again. This counts those deferrals by the same URL hash the seen check uses;
# at config.MAX_SCORING_DEFERRALS the posting is recorded at score 0 instead.
# The entry is removed once the posting is scored or recorded, and otherwise
# ages out with everything else in purge_old_entries.

DEFER_PREFIX = "d:"


def deferral_key(url):
    return DEFER_PREFIX + job_hash(url)


def load_deferrals(state=None):
    """{deferral key: count} for every posting with deferred scoring."""
    state = _load() if state is None else state
    return {k: int(v.get("count", 0)) for k, v in state.items()
            if k.startswith(DEFER_PREFIX) and isinstance(v, dict)}


def save_deferrals(counts, cleared, when=None):
    """Write new deferral counts and drop entries for postings now scored or recorded."""
    if not counts and not cleared:
        return
    state = _load()
    stamp = (when or datetime.utcnow()).isoformat()
    for key, count in counts.items():
        state[key] = {"ts": stamp, "count": count}
    for key in cleared:
        state.pop(key, None)
    _save(state)


def purge_old_entries():
    state = _load()
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    purged = {}
    for key, entry in state.items():
        ts = _entry_ts(entry)
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts) > cutoff:
                purged[key] = entry
        except ValueError:
            continue
    _save(purged)
    return len(state) - len(purged)


def is_first_run():
    return not os.path.exists(STATE_FILE) or _load() == {}


def _load_skip_state():
    if os.path.exists(SKIP_STATE_FILE):
        with open(SKIP_STATE_FILE, "r") as f:
            data = json.load(f)
        return {
            "run_days": data.get("run_days", []),
            "skipped_days": data.get("skipped_days", []),
        }
    return {"run_days": [], "skipped_days": []}


def _save_skip_state(skip_state):
    write_json_atomic(SKIP_STATE_FILE, skip_state, indent=2)


def record_run_day(had_new_jobs):
    """Record today's run for the weekly summary. A day whose runs produced zero
    new jobs counts as skipped (no daily email); a later same-day run that does
    find new jobs un-skips it. "Today" is the profile's delivery.timezone day."""
    import profile_loader
    today = profile_loader.local_today().isoformat()
    skip_state = _load_skip_state()
    if today not in skip_state["run_days"]:
        skip_state["run_days"].append(today)
    if had_new_jobs:
        skip_state["skipped_days"] = [d for d in skip_state["skipped_days"] if d != today]
    elif today not in skip_state["skipped_days"]:
        skip_state["skipped_days"].append(today)
    _save_skip_state(skip_state)


def get_skip_stats():
    """Return (skipped_day_count, run_day_count) since the last weekly reset."""
    skip_state = _load_skip_state()
    return len(skip_state["skipped_days"]), len(skip_state["run_days"])


def reset_skip_stats():
    """Clear skip tracking — called after each weekly summary is sent."""
    _save_skip_state({"run_days": [], "skipped_days": []})


def last_run_date():
    """Return the most recent mark_seen timestamp as a datetime, or None if never run."""
    state = _load()
    if not state:
        return None
    stamps = [ts for ts in (_entry_ts(entry) for entry in state.values()) if ts]
    try:
        return max(datetime.fromisoformat(ts) for ts in stamps)
    except (ValueError, TypeError):
        return None


# --- OPS-04 shard state --------------------------------------------------------
# Written only by fetch runs (fetch.yml). Shard selection reads it, so a shard
# whose run failed, timed out, or never started keeps the oldest last-success
# and is the one the next fetch slot takes -- retried, not dropped for the cycle.

def _shard_state_file():
    from config import SHARD_STATE_FILE
    return SHARD_STATE_FILE


def load_shard_state(shards):
    """{"shards": N, "last_success": {"0": iso, ...}, "daily_sources": {name: iso}}.

    N is config.FETCH_CYCLE_SLOTS. Changing it reassigns every board, so slot
    history for a different N is discarded; daily-source history is kept.
    """
    path = _shard_state_file()
    data = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    # PUB-04: daily sources are keyed by module key; a file written before
    # names them "Adzuna", "BuiltIn"... and is read as the keys (the next save
    # writes them back as keys), so no daily source is re-run for the rename.
    from config import source_key
    daily = {source_key(name): when for name, when in data.get("daily_sources", {}).items()}
    fresh = {"shards": shards, "last_success": {}, "daily_sources": daily}
    if data.get("shards") != shards:
        return fresh
    fresh["last_success"] = data.get("last_success", {})
    return fresh


def save_shard_state(shard_state):
    write_json_atomic(_shard_state_file(), shard_state, indent=2, sort_keys=True)


def next_shard(shard_state):
    """The cycle slot with the oldest last success; never-fetched first, lowest index on ties."""
    shards = shard_state["shards"]
    last = shard_state["last_success"]
    return min(range(shards), key=lambda i: (last.get(str(i), ""), i))


def record_shard_success(shard_state, shard, when=None):
    shard_state["last_success"][str(shard)] = (when or datetime.utcnow()).isoformat()


def last_daily_slot(slot_utc, now=None):
    """The most recent occurrence of the daily fetch slot (hour, minute UTC) at or before now."""
    now = now or datetime.utcnow()
    hour, minute = slot_utc
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


def daily_source_due(shard_state, name, slot_utc, now=None):
    """Due if the source has not succeeded since the most recent daily slot.

    Scheduled runs start hours after their cron time, so this is keyed on the
    slot, not on which cron fired: the 04:17 run (whenever it actually starts)
    takes the daily sources, later runs that day find them done, and a failed
    04:17 run leaves them due for the next slot.
    """
    last = shard_state["daily_sources"].get(name)
    if not last:
        return True
    return datetime.fromisoformat(last) < last_daily_slot(slot_utc, now)


def record_daily_source_success(shard_state, name, when=None):
    shard_state["daily_sources"][name] = (when or datetime.utcnow()).isoformat()
