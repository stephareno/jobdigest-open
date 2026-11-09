# config.py — mechanism only
#
# Budgets, shard settings, retries, file paths, the Sheet layout, and source
# URLs and models that are the same for every job seeker. Personal values live
# in profile.yaml (read through profile_loader): the title gate's terms, which
# sources are gated, industries and hard blocks, pay, every score and
# threshold, the scoring model, the dream-company list, what each source
# searches for and when and where the digest goes. Pinned Workday tenants and
# careers pages live in data/companies.yaml under pinned:.

import os
import re
from urllib.parse import quote_plus

import profile_loader

_ROLES = profile_loader.active()["roles"]

# --- Title gate (PRD §3) -----------------------------------------------------
# A cost control, not a quality filter: it only keeps titles that plainly are
# not the target role from spending scoring tokens. The JD decides fit.
#
# A title passes if it names an executive role outright
# (roles.target_titles) or carries a seniority term AND a function
# term (roles.seniority_terms, roles.function_terms). Words in
# roles.function_terms_never_alone never count on their own, only inside a
# longer phrase, so "Head of Enterprise Sales" stays out.
#
# main.add() applies it post-fetch to every source in TITLE_GATED_SOURCES
# (roles.title_gated_sources); the ATS and startup parsers apply it while
# parsing so detail requests are only spent on titles that will be scored. A
# source joins the list only if backtesting the gate against the Sheet loses
# zero historical score-7+ rows from it.


def _terms_re(terms):
    # Longest-first so "Corporate IT" is tried before bare "IT"; the lookarounds
    # keep "IT" from matching inside words like "unit" and "VP" inside "AVP".
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![\w.])(?:" + alts + r")(?!\w)", re.I)


def make_title_gate(roles):
    """The title gate for a profile's roles section: title -> bool."""
    never_alone = {w.strip().casefold() for w in roles["function_terms_never_alone"]}
    executive = _terms_re(roles["target_titles"])
    seniority = _terms_re(roles["seniority_terms"])
    function = _terms_re([t for t in roles["function_terms"] if t.strip().casefold() not in never_alone])

    def gate(title):
        if not title:
            return False
        if executive.search(title):
            return True
        return bool(seniority.search(title) and function.search(title))
    return gate


def make_watchlist_gate(roles):
    """The older, wider gate the watchlist keeps (roles.watchlist_gate_terms):
    one seniority word AND one function word, whole words."""
    terms = roles["watchlist_gate_terms"]
    seniority = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms["seniority"]) + r")\b", re.I)
    function = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms["function"]) + r")\b", re.I)
    return lambda title: bool(seniority.search(title)) and bool(function.search(title))


_title_gate = make_title_gate(_ROLES)
watchlist_title_gate = make_watchlist_gate(_ROLES)


def title_gate(title):
    """The PRD §3 title gate under the committed profile — see above."""
    return _title_gate(title)


def title_matches(title):
    """Kept for Built In (and the parked Wellfound fetcher), which import this name; same gate."""
    return title_gate(title)



# --- Source search settings (PUB-05) -------------------------------------------
# What each source searches for is in the profile's sources section. Two lists
# can be left out there and are derived from the location section instead.


def adzuna_locations(profile=None):
    """sources.adzuna.locations, or when the profile leaves it out: the home
    city, each other commutable city, then "Remote" (one Adzuna call per query
    per location, so the length is a cost)."""
    p = profile or profile_loader.active()
    listed = p["sources"]["adzuna"].get("locations")
    if listed:
        return list(listed)
    loc = p["location"]
    cities = [profile_loader.place_city(loc["home"])]
    for entry in loc["commutable"]:
        city = profile_loader.place_city(entry)
        if city.casefold() not in {c.casefold() for c in cities}:
            cities.append(city)
    return cities + ["Remote"]


# Built In's metro sites, by profile metro name ("City, ST"). Each slug was
# checked on 2026-10-28: builtin.com/jobs/<slug> titles its page with the
# metro; an unknown slug falls back to the national page. A metro missing here
# (Denver, Dallas, San Jose...) has no Built In site of its own.
BUILTIN_METRO_SLUGS = {
    "Atlanta, GA": "atlanta", "Austin, TX": "austin", "Baltimore, MD": "baltimore",
    "Boston, MA": "boston", "Charlotte, NC": "charlotte", "Chicago, IL": "chicago",
    "Detroit, MI": "detroit", "Houston, TX": "houston", "Los Angeles, CA": "los-angeles",
    "Miami, FL": "miami", "Nashville, TN": "nashville", "New Orleans, LA": "new-orleans",
    "New York City, NY": "new-york-city", "Philadelphia, PA": "philadelphia",
    "Pittsburgh, PA": "pittsburgh", "Portland, OR": "portland",
    "Salt Lake City, UT": "salt-lake-city", "San Diego, CA": "san-diego",
    "San Francisco, CA": "san-francisco", "Seattle, WA": "seattle",
    "Washington, DC": "washington-dc",
}
BUILTIN_REMOTE_SLUG = "remote"
# PUB-07b: Built In ignores ?title= and returns its unfiltered list; ?search= filters.
BUILTIN_SEARCH_URL = "https://builtin.com/jobs/{slug}?search={query}&seniority[]=senior-leader"


def builtin_search_urls(profile=None):
    """sources.builtin.search_urls, or when the profile leaves it out: each
    sources.builtin.title_queries entry on the home metro's Built In site (if
    it has one) and on Built In remote, query by query."""
    p = profile or profile_loader.active()
    listed = p["sources"]["builtin"].get("search_urls")
    if listed:
        return list(listed)
    home = profile_loader._place_name(p["location"]["home"])
    slugs = [s for s in (BUILTIN_METRO_SLUGS.get(home), BUILTIN_REMOTE_SLUG) if s]
    return [BUILTIN_SEARCH_URL.format(slug=slug, query=quote_plus(q))
            for q in p["sources"]["builtin"]["title_queries"] for slug in slugs]


# --- Source keys (PUB-04) ----------------------------------------------------
# Inside the code a source is named by its module in sources/ ("adzuna",
# "linkedin_email", "startups"), everywhere: job["source"], the profile's
# roles.title_gated_sources and sources.enabled, the budgets and priority
# below, shard_state.json. SOURCE_DISPLAY_NAMES is the only place the other
# spelling exists: the Sheet "Source" column and the digest show these exact
# strings, which every historical Sheet row carries. Code converts at the
# edges -- source_display() when writing the Sheet or the email, source_key()
# when reading a Sheet row, a queued batch or shard_state.json written before
# PUB-04 -- and never compares a display name.
SOURCE_DISPLAY_NAMES = {
    "adzuna": "Adzuna",
    "jsearch": "JSearch",
    "linkedin_email": "LinkedIn",
    "builtin": "Built In",
    "ats": "ATS",
    "watchlist": "Watchlist",
    "startups": "Startup",
    "bespoke": "Bespoke",
    "usajobs": "USAJOBS",
    "wellfound": "Wellfound",
    "applied_doc": "Index Doc",   # sources/applied_doc.py: rows seeded from the Job Index Doc
}
# Display name -> key, plus "BuiltIn": main.py, config and shard_state.json
# called Built In that before PUB-04 while its jobs carried "Built In".
_SOURCE_KEYS = {**{v: k for k, v in SOURCE_DISPLAY_NAMES.items()}, "BuiltIn": "builtin"}


def source_key(name):
    """The module key for a source name in either spelling; anything else
    (blank, an unknown label) comes back unchanged."""
    name = (name or "").strip() if isinstance(name, str) else name
    if name in SOURCE_DISPLAY_NAMES:
        return name
    return _SOURCE_KEYS.get(name, name)


def source_display(name):
    """What the Sheet and the digest show for a source key (or a name already
    in display form); an unknown value comes back unchanged."""
    return SOURCE_DISPLAY_NAMES.get(source_key(name), name)


TITLE_GATED_SOURCES = frozenset(_ROLES["title_gated_sources"])

# --- Run-time budgets (PRD: the run must complete and send even when a source
# is slow or unreachable). The daily job's hard timeout is 90 min; fetching is
# capped well inside it so scoring, the Sheet and the email always get to run.
FETCH_BUDGET_SECS = 45 * 60          # all sources together
SOURCE_BUDGET_SECS = {               # per source key; a source over budget is abandoned
    "adzuna": 240, "jsearch": 240, "linkedin_email": 480,
    "builtin": 180,
    "ats": 600, "watchlist": 600,
    "startups": 10 * 60 + 120,       # STARTUP_FETCH_BUDGET_SECS + in-flight grace
    # Bespoke stops itself at BESPOKE_RUN_BUDGET_SECS (300); this outer limit
    # only catches a hang, and an abandoned Bespoke run saves nothing.
    "bespoke": 300 + 60,
    "usajobs": 180,                  # SRC-20: ~7 searches, a few pages each
}
STARTUP_FETCH_BUDGET_SECS = 10 * 60  # startup tier stops picking up boards after this
STARTUP_FETCH_WORKERS = 12           # concurrent board fetches in the startup tier

# --- OPS-04: sharded fetch, digest on its own schedule (PRD §2.9, §7) ---------
# fetch.yml runs FETCH_RUNS_PER_DAY times a day. The startup tier cycles over
# FETCH_CYCLE_SLOTS runs (two days). Each board has a stable hash of its
# ats:slug. A board on a daily vendor is fetched in the runs where
# hash % FETCH_RUNS_PER_DAY matches, i.e. once a day; a board on a two-day
# vendor only where hash % FETCH_CYCLE_SLOTS matches, i.e. once every two days.
FETCH_RUNS_PER_DAY = 3
FETCH_CYCLE_SLOTS = 2 * FETCH_RUNS_PER_DAY
# Most boards one fetch run may carry: ~80% of STARTUP_FETCH_BUDGET_SECS at the
# measured throughput (16 Sep: 5,179 boards in 322 s locally, ~16 boards/s;
# ~14 boards/s on the runner) -> 0.8 x 600 s x ~12.5 boards/s, rounded down.
STARTUP_RUN_MAX_BOARDS = 6000
# Every vendor is fetched daily while the largest run fits STARTUP_RUN_MAX_BOARDS.
# When it does not, vendors move to the two-day cycle in this order, first to
# move first, until it fits: lowest new-posting rate first (DEL-05 survey,
# 16 Sep -- one day, 25 boards per vendor; a starting point to revisit once the
# Sheet has Posted Date data). The SRC-13 vendors had no survey data and sit
# before the four that move last. Workday, Oracle HCM, SmartRecruiters and
# Greenhouse move last. A vendor missing from this list is never moved.
TWO_DAY_VENDOR_ORDER = (
    "personio", "bamboohr", "rippling", "recruitee", "teamtailor", "ashby",
    "breezy", "lever", "workable",
    "adp", "comeet", "gem", "jobscore", "manatal", "ukg_ready",
    "workday", "oracle_hcm", "smartrecruiters", "greenhouse",
)
# Sources fetched once a day, anchored to the 04:17 UTC fetch slot -- the one
# that lands before the 06:17 UTC digest. A source is due when its last success
# is older than the most recent DAILY_SOURCE_SLOT_UTC, so the 04:17 run takes
# them, later runs that day skip them, and if the 04:17 run fails (or never
# records a success) the next slot takes them instead. A manual dispatch of
# fetch.yml always takes them (`main.py fetch --daily-sources`). Adzuna and
# JSearch have API quotas, so their daily call count must not grow; the rest
# gain nothing from intraday runs while the digest is sent once a day.
DAILY_SOURCES = ("adzuna", "jsearch", "linkedin_email", "builtin", "ats", "watchlist", "usajobs")
# Sources taken on every fetch run. Startup is sharded across runs; Bespoke
# continues its rolling cursor, so skipping a run would only slow the pass.
EVERY_RUN_SOURCES = ("startups", "bespoke")
DAILY_SOURCE_SLOT_UTC = (4, 17)  # (hour, minute); keep in step with fetch.yml's first cron hour

# Short labels for times shown in the digest ("12:17 AM ET"), by IANA zone. A
# zone missing here shows its abbreviation ("EDT", "CET") instead.
TZ_LABELS = {
    "America/New_York": "ET", "America/Detroit": "ET", "America/Chicago": "CT",
    "America/Denver": "MT", "America/Phoenix": "MT", "America/Los_Angeles": "PT",
    "America/Anchorage": "AKT", "Pacific/Honolulu": "HT",
}

# --- Digest and weekly-summary schedule (PUB-05/PUB-08, `python main.py cron`) -
# The digest cron fires this many minutes before the profile's
# delivery.digest_time_local, in local time. CLAUDE.md rule 8: size the lead
# for the p90 GitHub queue delay, not the median -- to 2026-09-10 the last 20
# scheduled runs waited median 174 min, p90 254 min, worst 559 min (the 15:00
# UTC slot alone p90 314 min). 343 min = 5 h 43 min puts an 08:00 digest's
# cron at 02:17 local, the 06:17 UTC (EDT) / 07:17 UTC (EST) daily.yml has
# always carried: 314 min of p90 delay plus runtime still lands before 8:00.
CRON_LEAD_MINUTES = 343
# The weekly summary's local time is the profile's
# delivery.weekly_summary_time_local (PUB-08; was a constant here).
# GitHub's scheduler is most congested at the top of the hour; a cron that
# would land on minute 0 fires this many minutes earlier instead.
CRON_OFF_THE_HOUR_MINUTES = 3
# PUB-08 slot guard. A DST zone's workflow carries one cron line per UTC
# offset and every line fires; a --scheduled run goes ahead only when it fired
# within this many minutes of the intended fire time (schedule.on_slot). The
# two lines are 60 minutes apart, so 30 splits them down the middle. GitHub
# queue delay makes a run START late, never early -- by hours here (see
# CRON_LEAD_MINUTES) -- so the guard never compares the start time or the
# delivery time: it compares the instant the run's own cron line last matched
# (github.event.schedule, passed as SCHEDULED_CRON) with the intended fire time.
SLOT_GUARD_MINUTES = 30

# FIL-10: when two sources hand us the same opening, this decides which copy
# survives and therefore which apply link is stored. It is CLAUDE.md rule 6's
# link priority — company site > LinkedIn > Indeed/ZipRecruiter > aggregator —
# read as sources: the direct ATS and careers-page fetchers give a company URL,
# LinkedIn gives a LinkedIn one, Built In is a job board, JSearch and Adzuna
# are aggregators. This is NOT main.SOURCES order, which is fetch order.
SOURCE_PRIORITY = ("ats", "startups", "watchlist", "bespoke", "usajobs",
                   "linkedin_email", "builtin", "jsearch", "adzuna")


def source_rank(name):
    """Position of a source in SOURCE_PRIORITY; an unknown source ranks last.
    Takes the key; a pre-PUB-04 name is converted first (source_key)."""
    try:
        return SOURCE_PRIORITY.index(source_key(name))
    except ValueError:
        return len(SOURCE_PRIORITY)


# FIL-13: the digest shows one card per hiring company + title, across this
# batch and the main-index rows first seen in the last this-many days. Email
# only; every row is still written to the Sheet.
HIRING_COLLAPSE_LOOKBACK_DAYS = 14

SHARD_STATE_FILE = "shard_state.json"   # per-cycle-slot + per-daily-source last success; written by fetch runs only
PENDING_DIR = "state/pending"           # scored, not-yet-emailed postings; one immutable file per fetch run

# DEL-14: company profiles for the digest blurb (profiles.py). Before the email
# is built, each emailed role's company is read once from its own website and
# the blurb's company and culture content is rewritten from that profile plus
# the posting. Profiles never feed the score. Cached per domain in
# PROFILE_CACHE_FILE, written only by the digest (daily.yml commits it).
PROFILE_CACHE_FILE = "company_profiles.json"
PROFILE_TTL_DAYS = 90
# A site that gave no usable text (JS-only, blocked) is retried sooner.
PROFILE_EMPTY_TTL_DAYS = 30
# The whole step: fetches, profile calls and blurb rewrites. Anything not done
# by then keeps its posting-only (DEL-13) blurb.
PROFILE_BUDGET_SECS = 90
PROFILE_COMPANY_BUDGET_SECS = 8
PROFILE_MAX_IN_FLIGHT = 8
# Homepage plus the first PROFILE_MAX_SUBPAGES of these that answer with text.
PROFILE_PATHS = ("/about", "/about-us", "/company", "/careers", "/culture", "/values", "/mission")
PROFILE_MAX_SUBPAGES = 2
# Under this much visible text a page counts as nothing (a JS-rendered shell).
PROFILE_MIN_PAGE_CHARS = 300
PROFILE_MAX_TEXT_CHARS = 12_000    # page text sent to the profile call
# The utility model: company profiles (profiles.py). Cheap extraction, not
# scoring. Parsing Job Index Doc entries (sources/applied_doc.py) uses
# scoring.model: it creates Applied rows, so it is not cheap to get wrong.
PROFILE_MODEL = "claude-haiku-4-5"
# The blurb rewrite uses the profile's scoring.model, so the voice matches.
BLURB_MAX_WORDS = 45

# TRK-11 (PRD §15): the status scanner reads each candidate email with Claude.
# The cheapest model that passes tests/fixtures/status_emails/ (scorer model
# is the fallback default). Re-record the fixtures when changing it:
#   python tools/record_status_fixtures.py --model <id>
STATUS_EMAIL_MODEL = "claude-haiku-5-5"
# Thinking off: a one-email classification, and Haiku 5.5's default thinking
# doubles output tokens with no change on the fixtures. None omits the
# parameter (the model's default); some models reject "disabled".
STATUS_EMAIL_THINKING = {"type": "disabled"}
STATUS_EMAIL_MAX_BODY_CHARS = 20_000  # email body sent per call (links already shortened)
STATUS_EMAIL_MAX_ROWS = 30            # candidate rows sent per call
STATUS_EMAIL_DRY_RUN_DAYS = 60        # --dry-run replay window
STATUS_EMAIL_MAX_IN_FLIGHT = 8        # concurrent classification calls (~500 per 14-day run)
# TRK-12: Claude's reply per email, reused while the prompt is unchanged.
# Written only by the digest (daily.yml commits it), never by fetch.yml.
STATUS_EMAIL_CACHE_FILE = "status_email_cache.json"

# DEL-05 (PRD §7): a role is flagged fresh in the digest when posted within
# this many hours (time sources) or today/yesterday in ET (date-only sources).
FRESH_WINDOW_HOURS = 24
# ATS vendors whose posted date is shown but never earns the fresh flag:
# Oracle HCM's PostedDate can reset when a requisition is reposted.
NO_FRESH_FLAG_VENDORS = frozenset({"oracle_hcm"})

# OPS-12: a posting whose scoring call fails on a transient API error is
# retried on this many later runs before it is recorded at score 0.
MAX_SCORING_DEFERRALS = 3
# SCO-09: the scoring call classifies the function a role leads. Any value but
# FUNCTION_FIT_LEADERSHIP caps the final score at the profile's
# scoring.function_cap, in code (location.apply_location_tier), after every
# other adjustment. Below scoring.thresholds.worth_a_look, so a capped role is
# logged, never emailed. Score thresholds, comp (comp.py) and the caps are all
# in profile.yaml.
#
# PUB-10: generic names. The vocabulary before PUB-10 is still read (queued
# batches, a reply from an older prompt) through function_fit_value().
FUNCTION_FIT_LEADERSHIP = "leads_department"
FUNCTION_FIT_VALUES = ("leads_department", "in_department", "outside_department")
LEGACY_FUNCTION_FIT = {"it_leadership": "leads_department", "it_other": "in_department",
                       "not_it": "outside_department"}


def function_fit_value(value):
    """A function_fit as one of FUNCTION_FIT_VALUES (an old name mapped to its
    new one), "unknown" for anything else, or None when there is none (a job
    scored before SCO-09)."""
    if value is None:
        return None
    text = str(value).strip().lower()
    text = LEGACY_FUNCTION_FIT.get(text, text)
    return text if text in FUNCTION_FIT_VALUES else "unknown"

# --- SRC-20: USAJOBS (PRD §2.12) ------------------------------------------------
# Federal postings, fetched once a day. Not title-gated (PRD §3); bounded by job
# series, hiring path and the pay floor (the profile's
# sources.usajobs.pay_floor_from, on the top of the annual range).
# Series, hiring paths and title keywords are the profile's sources.usajobs.
USAJOBS_DATE_POSTED_DAYS = 7           # dedup absorbs the overlap between runs
USAJOBS_RESULTS_PER_PAGE = 500         # the API maximum
USAJOBS_MAX_PAGES = 10                 # per search, safety cap

# The dream-company list is the file the profile's dream_companies.file names
# (profile_loader.dream_companies()). Workday tenants and careers-page URLs for
# the ones with no ATS board are in data/companies.yaml under pinned:.

# Companies that are job boards / aggregators, not actual employers.
# Jobs where the company field matches any of these are dropped post-fetch.
JOB_BOARD_COMPANY_BLOCKLIST = [
    "VirtualVocations", "Virtual Vocations", "Jobgether", "Lensa",
    "Talentify", "Tideri", "Jobtensor", "Adzuna", "Indeed", "ZipRecruiter",
    "Glassdoor", "Monster", "CareerBuilder", "SimplyHired", "Dice",
    "Snagajob", "The Ladders", "Hired", "Toptal", "Salary.com",
    "Jooble", "Joblist", "Workable", "SmartRecruiters",
]

# PUB-10: the tab names and header rows live in sheet_layout.py (no profile
# needed, for sheet-init).
from sheet_layout import SHEET_COLUMNS, SHEET_NAME, DISQUALIFIED_SHEET_NAME  # noqa: E402,F401

STATUS_NEW = "New"
STATUS_INTERESTED = "Interested"
STATUS_APPLIED = "Applied"
STATUS_INTERVIEWING = "Interviewing"
STATUS_NO_ACTION = "No action taken"
STATUS_DECLINED = "Declined"
STATUS_CLOSED = "Closed"

GMAIL_IMAP_SERVER = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# ---------------------------------------------------------------------------
# Bespoke careers-page source (PRD §2.10) — sources/bespoke.py
#
# Companies with no fetchable ATS board, monitored straight from their own
# careers page. Two kinds live here and are treated identically, because both
# render as readable HTML: pages with no hiring vendor at all (`bespoke_page`),
# and pages on a vendor whose API we cannot reach (iCIMS, ADP, Paylocity,
# JazzHR, and the rest of the unsupported_ats:* set).
#
# Cost control is change detection, not selection: every company is checked on
# the rolling schedule regardless of headcount, sector, or whether it has
# anything posted today, and the model is only paid for a page that changed.
# ---------------------------------------------------------------------------

BESPOKE_ENABLED = True
BESPOKE_SOURCE_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "sourcing", "startups_unresolved.csv")
BESPOKE_STATE_FILE = "bespoke_state.json"

# Reasons whose pages are worth reading. Everything with a careers_url except
# the no_site rows, which by definition have no page. Prefix match, so every
# unsupported_ats:<vendor> is included without listing each vendor.
BESPOKE_REASON_PREFIXES = ("bespoke_page", "unsupported_ats:", "stale_board",
                           "content_check_failed", "board_unavailable:")
# Domain marketplaces and parking pages. A careers_url on one of these is a
# lapsed or guessed domain -- a page that can never list jobs -- so Bespoke
# skips it rather than pay for a first read (SRC-17: ~190 rows, 21 Sep).
BESPOKE_SKIP_HOSTS = ("hugedomains.com", "namepros.com", "brandbucket.com", "dynadot.com",
                      "name.si", "domaineasy.com", "sedo.com", "afternic.com", "dan.com",
                      "undeveloped.com", "squadhelp.com", "buydomains.com",
                      "atmholdings.com")  # SRC-19: bolt.com redirects to its sale page

# Rolling pass (PRD §2.10). Each fetch run continues through the company list
# from a saved cursor (BESPOKE_CURSOR_FILE) and takes as many companies as fit
# BESPOKE_RUN_BUDGET_SECS; the next run resumes where it stopped. Only fully
# processed companies are saved, and only after main.py has queued their
# postings. Order is a stable hash of the company name, so rewriting the CSV
# neither reshuffles the pass nor moves the cursor.
BESPOKE_CURSOR_FILE = "bespoke_cursor.json"
BESPOKE_RUN_BUDGET_SECS = 300
# Don't start a check batch, or an extraction call, this close to the budget:
# a batch can take up to a few BESPOKE_TIMEOUTs, a call up to its timeout.
BESPOKE_CHECK_BATCH = 64
BESPOKE_BATCH_RESERVE_SECS = 45
BESPOKE_EXTRACT_RESERVE_SECS = 35
BESPOKE_EXTRACT_TIMEOUT = 30

# Below this many visible words the page is a JavaScript shell that renders
# nothing server-side. Counted and reported, never extracted — reading them
# needs a headless browser, which is a separate decision.
BESPOKE_MIN_VISIBLE_WORDS = 150

BESPOKE_MAX_IN_FLIGHT = 16      # concurrent page fetches
BESPOKE_HOST_DELAY = 1.0        # seconds between requests to the same host
BESPOKE_TIMEOUT = 8.0
BESPOKE_MAX_PAGE_BYTES = 2_000_000

# --- LinkedIn job pages (REV-07) --------------------------------------------------
# LinkedIn often blocks GitHub's runners (HTTP 999) or shows a login wall. A job
# whose page does not load is dropped from the run, unseen, and retried on a
# later run (the alert stays in the label; the 7-day lookback covers it).
# True keeps such a job anyway, with an empty description, when the alert
# email itself gives its title, company and location. Off by default: the
# scorer then sees no description, so keeping it is a deliberate choice.
LINKEDIN_KEEP_ALERT_ONLY_ROWS = False
# After this many runs in a row with a failed page, the job is recorded at
# score 0 (Disqualified tab) with this reason, so it stops costing fetches.
LINKEDIN_MAX_PAGE_FAILURES = 3
LINKEDIN_UNAVAILABLE_REASON = "LinkedIn page unavailable"

# Extraction is the only paid step and runs only on a changed page.
BESPOKE_EXTRACT_MODEL = "claude-haiku-4-5"
BESPOKE_MAX_DIGEST_CHARS = 24_000
BESPOKE_MAX_ANCHORS = 120
# Extractions run this many at a time.
BESPOKE_EXTRACT_CONCURRENCY = 4
# REV-06: at most this many extraction calls in one fetch run, unless the
# profile's sources.bespoke.max_extractions_per_run says otherwise. Pages past
# it are left for the next pass, the cursor unchanged. About $0.003 each at
# BESPOKE_EXTRACT_MODEL (SRC-18), so 60 is under $0.20 a run. Extractions
# also count toward scoring.max_calls_per_day (fetch_budget.py).
BESPOKE_MAX_EXTRACTIONS_PER_RUN = 60

# --- Cost reporting (PUB-12) -------------------------------------------------------------
# Anthropic first-party API prices, US$ per million tokens (input, output), as
# published on 2026-10-06. Prices change: check the pricing page before quoting a
# figure, and update this table. A model missing here reports its cost as
# "unknown" (model_cost returns None), never as $0. Used by calibrate, doctor,
# resolve's preview, profiles.py's run-log line and the website ladder's guess step.
MODEL_PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}


# REV-06: prompt-cache prices, US$ per million tokens (5-minute cache write,
# cache hit), from the same Anthropic pricing page as MODEL_PRICES, read on
# 2026-11-09. Most models write at 1.25x and read at 0.1x their input price,
# but not all (Fable 5.1 reads at 0.025x, Opus 5.5 and Sonnet 5.5 at 0.05x),
# so each model's figures are listed rather than derived.
MODEL_CACHE_PRICES = {
    "claude-fable-5-1": (12.50, 0.25),
    "claude-fable-5": (12.50, 1.00),
    "claude-opus-5-5": (5.00, 0.20),
    "claude-opus-5": (6.25, 0.50),
    "claude-opus-4-8": (6.25, 0.50),
    "claude-opus-4-7": (6.25, 0.50),
    "claude-opus-4-6": (6.25, 0.50),
    "claude-sonnet-5-5": (2.50, 0.10),
    "claude-sonnet-5": (2.50, 0.20),
    "claude-sonnet-4-6": (3.75, 0.30),
    "claude-haiku-5-5": (0.125, 0.01),
    "claude-haiku-4-5": (1.25, 0.10),
}


def model_cost(model, input_tokens, output_tokens, cache_write_tokens=0, cache_read_tokens=0):
    """US$ for this many tokens on `model`, or None when the table does not know it.
    input_tokens is the uncached input (the API's input_tokens); cache writes
    and reads are priced from MODEL_CACHE_PRICES (REV-06)."""
    price = MODEL_PRICES.get(model)
    if price is None:
        return None
    write, read = MODEL_CACHE_PRICES.get(model, (None, None))
    if (cache_write_tokens and write is None) or (cache_read_tokens and read is None):
        return None
    return (input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]
            + cache_write_tokens / 1e6 * (write or 0) + cache_read_tokens / 1e6 * (read or 0))


def format_cost(dollars):
    """"$0.0123", or "unknown" for None."""
    if dollars is None:
        return "unknown"
    return f"${dollars:.4f}" if dollars < 1 else f"${dollars:,.2f}"


# Tokens one Bespoke first read takes (input, output): SRC-18 measured about
# $0.003 per Haiku 4.5 extraction, which is this pair at Haiku 4.5's prices.
# resolve's preview prices a new careers page with it at BESPOKE_EXTRACT_MODEL.
BESPOKE_FIRST_READ_TOKENS = (2_000, 200)

# --- resolve (PUB-12) ---------------------------------------------------------------------
# Wall time per company for `main.py resolve`, measured in PUB-11: 200 public
# companies in 842 s at the resolver's concurrency of 8 (slug probes plus the
# careers-page pass). The preview's estimate is this times the companies to try.
RESOLVE_SECS_PER_COMPANY = 4.2
# Per-vendor backoff on HTTP 429: the wait starts at the base, doubles on each
# 429 in a row from that vendor, never exceeds the ceiling, and resets on any
# other answer. After RESOLVE_429_RETRIES a request gives up: the company is
# counted as deferred for that vendor and is not marked tried, so the next run
# takes it again.
RESOLVE_429_BASE_SECS = 2.0
RESOLVE_429_CEILING_SECS = 60.0
RESOLVE_429_RETRIES = 4
# A vendor whose requests give up this many times in a row is cut off for the
# rest of the run (its companies deferred), so it cannot stall a batch.
RESOLVE_429_CUTOFF = 3
# Workable's own paced queue (resolve.WorkablePacer, PUB-12), only for companies
# with no known website: Workable blocks address ranges, so the careers page is
# the way to find its tenants. One request every START seconds, the gap doubled
# on each 429 up to CEILING and shrunk by RECOVER per answered request (never
# below START); GIVE_UP 429s in a row end Workable for the run. GRACE seconds
# after the rest of a batch is done, a company still queued is settled with a
# note: Workable never holds a company past the batch.
RESOLVE_WORKABLE_START_SECS = 10.0
RESOLVE_WORKABLE_CEILING_SECS = 60.0
RESOLVE_WORKABLE_RECOVER = 0.9
RESOLVE_WORKABLE_GIVE_UP = 5
RESOLVE_WORKABLE_GRACE_SECS = 60.0
# Tokens one website-ladder model guess takes (input, output), for the preview's
# estimate at PROFILE_MODEL: measured in PUB-12's public-list build, 1,671 calls
# at claude-haiku-4-5, 133,615 input and 35,780 output tokens ($0.31 in all).
WEBSITE_GUESS_TOKENS = (80, 21)
# Tokens one scoring call takes on average (input, output), measured by a live
# `calibrate` under the committed profile on 2026-10-29 (13 calls at
# claude-sonnet-4-6: 3,246 input and 205 output tokens a call, $0.0128 a call);
# doctor prices a call with it.
SCORING_TOKENS_PER_CALL = (3_246, 205)
