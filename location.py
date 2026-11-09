# location.py — post-fetch location filter, location tiers, relo tag, home bonus
#
# Rules (PRD §3, applied on required physical presence, not HQ):
#   Any work model, outside location.country → location.outside_country:
#       "disqualify" drops it pre-scoring; a number is added after scoring
#   Work model first (PUB-09): location.work_models.<remote|hybrid|onsite>, a
#       number or "disqualify"; a disqualify skips every step after it
#   Remote, anywhere in the country        → keep, no tier penalty
#   Hybrid or in-office (FIL-09), by straight-line distance:
#     within commutable_radius_miles of a commutable metro → keep, no penalty, no tag
#     within relocate_radius_miles of a relocate metro     → relocate_penalty, tag "relo"
#     commutable radius to outer_band_miles of a commutable metro
#                                                           → outer_band_penalty, tag "relo"
#     anywhere else in the US                               → elsewhere_penalty, tag "relo"
#     location the posting can't place     → fallbacks, in order:
#         scoring call's city/state (read from the JD and the company's offices),
#         bare town → most favourable tier among places of that name with
#             sizeable_place_population+ people (else the most populous);
#             "Washington" alone → Washington DC,
#         state only → tier most of the state's population falls in
#             (state_tier_overrides win),
#         "United States" / blank → no penalty, no tag
#   Every hybrid/onsite scored job's resolution method is logged once per run.
#   Unknown work model                      → pass through; scorer decides
#
# GAP-07: a hybrid or in-office role whose resolved point is within
# home_bonus_radius_miles of the home metro gets home_bonus, applied after the
# tier and before the floor and the cap. Remote roles get nothing, however
# close the company is, and the other commutable metros get nothing.
#
# The penalty applies to every hybrid/onsite role outside the commutable band,
# whatever the company and whatever the base score (FIL-11 removed the
# dream-company and base-score-8+ exemptions). A location penalty never takes a
# score below scoring.min_score_after_penalties.
#
# PUB-09: every tier penalty, the work-model outcomes, outside_country and the
# comp floor and unstated outcomes may be "disqualify" instead of a number: a
# dealbreaker. apply_location_tier applies them in the order work model ->
# location tier -> home bonus -> comp -> function cap -> floor and cap; the
# first dealbreaker sets score 0 and job["dealbreaker"], named in
# location_reason or the comp note ("dealbreaker: onsite"), and the role goes
# to the Disqualified tab like a hard block, never emailed.
#
# PUB-03: every metro, radius, penalty and the home bonus is read from the
# location section of profile.yaml into CONFIG (a LocationConfig), built once
# at the bottom of this module. Metros are "City, ST" names resolved against
# data/us_places.tsv.gz (GeoNames US places and counties, built by
# tools/build_us_places.py), read once per run. No town lists and no network
# calls. Only mechanism stays here as constants.

import re
from collections import Counter, namedtuple
from collections.abc import Mapping
from dataclasses import dataclass

from comp import COMP_NOTE_RE, comp_adjustment, comp_note, display_range
from profile_loader import DISQUALIFY
from config import FUNCTION_FIT_LEADERSHIP, function_fit_value
from functools import lru_cache

import profile_loader
# The places file and the location-string resolver (PUB-04: split out so
# profile_loader can validate metros without importing this module). Names
# re-exported here keep their location.<name> spelling for callers and tests.
from places import (  # noqa: F401
    PLACES_FILE, SAME_PLACE_MILES, Centre, Place, PlaceError, _ALT_SPLIT_RE, _COUNTRY_TOKENS,
    _NOISE_TOKENS, _STATE_BY_NAME, _STATES, _norm, _places, _pt, centre_for, miles_between,
    resolve_points, state_only,
)

# --- Tiers: read from profile.yaml ----------------------------------------------


@dataclass(frozen=True)
class LocationConfig:
    """profile["location"] (and the score bounds and function cap from profile["scoring"])
    with every metro resolved to a point. Frozen and hashable, so the
    per-config caches below can key on it."""
    home: Centre
    home_bonus: int
    home_bonus_radius_miles: int
    commutable: tuple                # Centres
    commutable_radius_miles: int
    outer_band_miles: int
    outer_band_penalty: int
    relocate: tuple                  # Centres
    relocate_radius_miles: int
    relocate_penalty: object         # int, or DISQUALIFY (as are the two other tier penalties)
    elsewhere_penalty: object
    state_overrides: tuple           # ((two-letter code, penalty), ...)
    sizeable_place_population: int
    country: str                     # location.country, the places file's country (PUB-09)
    outside_country: object          # location.outside_country: int or DISQUALIFY
    work_models: tuple               # (("remote", outcome), ("hybrid", ...), ("onsite", ...))
    score_floor: int                 # scoring.min_score_after_penalties
    max_score: int                   # scoring.max_score
    function_cap: int                # scoring.function_cap (SCO-09)
    criteria: tuple = ()             # ((id, points), ...) from scoring.role_criteria (PUB-09)
    dealbreakers: tuple = ()         # ids of the dealbreaker role criteria (PUB-10)
    dealbreaker_terms: tuple = ()    # ((id, (evidence term, ...)), ...) (PUB-19)

    def state_override(self, abbr):
        """The fixed penalty for a state-only location, or None."""
        return dict(self.state_overrides).get(abbr)

    def work_model_outcome(self, key):
        """location.work_models[key] (an int or DISQUALIFY); 0 for no key."""
        return dict(self.work_models).get(key, 0) if key else 0


def build_config(profile):
    """A LocationConfig from a loaded profile (or a plain dict of the same
    shape, for tests). Expects a validated profile; a metro that does not
    resolve raises PlaceError."""
    loc, scoring = profile["location"], profile["scoring"]
    commutable = tuple(centre_for(e) for e in loc["commutable"])
    home_name = loc["home"]["name"] if isinstance(loc["home"], Mapping) else loc["home"]
    # The home metro is also a commutable one (validated); use that Centre, so
    # a lat/lon override given on either entry applies to both.
    home = next((c for c in commutable if c.name.casefold() == home_name.casefold()), None)
    return LocationConfig(
        home=home or centre_for(loc["home"]),
        home_bonus=loc["home_bonus"],
        home_bonus_radius_miles=loc["home_bonus_radius_miles"],
        commutable=commutable,
        commutable_radius_miles=loc["commutable_radius_miles"],
        outer_band_miles=loc["outer_band_miles"],
        outer_band_penalty=loc["outer_band_penalty"],
        relocate=tuple(centre_for(e) for e in loc["relocate"]),
        relocate_radius_miles=loc["relocate_radius_miles"],
        relocate_penalty=loc["relocate_penalty"],
        elsewhere_penalty=loc["elsewhere_penalty"],
        state_overrides=tuple(sorted(loc["state_tier_overrides"].items())),
        sizeable_place_population=loc["sizeable_place_population"],
        country=loc.get("country", "US"),
        outside_country=loc.get("outside_country", DISQUALIFY),
        work_models=tuple((k, (loc.get("work_models") or {}).get(k, 0)) for k in ("remote", "hybrid", "onsite")),
        score_floor=scoring["min_score_after_penalties"],
        max_score=scoring["max_score"],
        function_cap=scoring["function_cap"],
        criteria=tuple((c["id"], c["points"]) for c in scoring.get("role_criteria") or ()
                       if not c.get("dealbreaker")),
        dealbreakers=tuple(c["id"] for c in scoring.get("role_criteria") or () if c.get("dealbreaker")),
        dealbreaker_terms=tuple((c["id"], tuple(c.get("evidence_terms") or ()))
                                for c in scoring.get("role_criteria") or () if c.get("dealbreaker")),
    )


# Set at the bottom of this module from profile.yaml. None only while the
# module is loading (build_config resolves names, which needs no tiers).
CONFIG = None

# A country signal wins over any foreign term (PUB-09: read for
# location.country, which validation keeps to the places file's country). For
# the US: protects "Paris, TX", "Dublin, OH", "Ontario, CA", "Melbourne, FL",
# "New Mexico", etc.
_US_SIGNAL_RE = re.compile(
    r"\b(united states|usa|u\.s\.a?\.?|new mexico)\b"
    r"|\bus\b"
    r"|,\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn"
    r"|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa"
    r"|wv|wi|wy|dc)\b"
)

_INTL_TERMS = [
    # countries & regions
    "united kingdom", "uk", "england", "scotland", "wales", "ireland",
    "canada", "germany", "france", "netherlands", "spain", "portugal",
    "italy", "poland", "czech", "austria", "switzerland", "belgium",
    "denmark", "sweden", "norway", "finland", "hungary", "romania",
    "greece", "india", "japan", "china", "hong kong", "taiwan",
    "singapore", "south korea", "korea", "australia", "new zealand",
    "brazil", "argentina", "chile", "colombia", "mexico", "costa rica",
    "israel", "uae", "saudi arabia", "egypt", "south africa", "nigeria",
    "kenya", "philippines", "vietnam", "indonesia", "thailand",
    "malaysia", "turkey", "ukraine", "emea", "apac", "latam",
    # major non-US cities
    "london", "toronto", "vancouver", "montreal", "berlin", "munich",
    "paris", "amsterdam", "madrid", "barcelona", "lisbon", "milan",
    "rome", "warsaw", "krakow", "prague", "vienna", "zurich", "geneva",
    "brussels", "copenhagen", "stockholm", "oslo", "helsinki",
    "budapest", "bucharest", "bangalore", "bengaluru", "mumbai",
    "hyderabad", "pune", "chennai", "gurgaon", "noida", "tokyo",
    "osaka", "shanghai", "beijing", "shenzhen", "taipei", "seoul",
    "sydney", "melbourne", "brisbane", "auckland", "sao paulo",
    "são paulo", "buenos aires", "santiago", "bogota", "mexico city",
    "guadalajara", "tel aviv", "dubai", "abu dhabi", "cairo",
    "johannesburg", "cape town", "lagos", "nairobi", "manila", "hanoi",
    "jakarta", "bangkok", "kuala lumpur", "istanbul", "kyiv",
]
# The country's own abbreviations and names, by location.country.
_COUNTRY_SIGNAL_RES = {"US": _US_SIGNAL_RE}

# dub\d*: Vantage Data Centers posts Dublin sites as bare facility codes ("DUB11").
# The terms above are foreign to the US, the one country the places file covers.
_INTL_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in _INTL_TERMS) + r"|dub\d*)\b")



LocationTier = namedtuple("LocationTier", "penalty relo reason")

_HYBRID_ONSITE = ("hybrid", "in-office")


def _loc(job):
    return (job.get("location") or "").lower()


def _model(job):
    return (job.get("work_model") or "").lower().strip()


def is_international(location):
    """True when the location reads as outside location.country. A country
    signal (for the US: a state code, "US", "United States") always wins."""
    if not location:
        return False
    if _COUNTRY_SIGNAL_RES[CONFIG.country if CONFIG else "US"].search(location):
        return False
    return bool(_INTL_RE.search(location))


def _rank(penalty):
    """A tier penalty as a number to compare tiers by; a dealbreaker is the worst."""
    return -10**6 if penalty == DISQUALIFY else penalty


def work_model_key(job):
    """The location.work_models key for the job's work model: "remote",
    "hybrid" or "onsite" (In-office, on-site), or None when unknown."""
    model = _model(job).replace("-", "").replace(" ", "")
    if model == "remote":
        return "remote"
    if model == "hybrid":
        return "hybrid"
    if model in ("inoffice", "onsite"):
        return "onsite"
    return None


def resolve_location(location, most_populous=False):
    """All US points a location string names, as a tuple of (lat, lon)
    (places.resolve_points). most_populous: a bare town name shared by
    far-apart places resolves to the most favourable tier under CONFIG among
    the sizeable ones, else the largest; "Washington" alone means DC."""
    return _resolve_location(location, most_populous, CONFIG)


# Keyed on the config too: the most_populous fallback picks a place by tier.
@lru_cache(maxsize=None)
def _resolve_location(location, most_populous, config):
    if not most_populous:
        return resolve_points(location)

    def rank(point):
        tier = tier_for_point(point, config)
        return (-_rank(tier.penalty), tier.relo)
    return resolve_points(location, most_populous=True,
                          sizeable_population=config.sizeable_place_population, rank=rank)


# --- Tiers ---------------------------------------------------------------------

def tier_for_point(point, config=None):
    """LocationTier for a resolved hybrid/onsite point (no exemptions)."""
    cfg = config or CONFIG
    home, home_mi = min(((c.name, miles_between(point, c.point)) for c in cfg.commutable),
                        key=lambda x: x[1])
    if home_mi <= cfg.commutable_radius_miles:
        return LocationTier(0, False, f"commutable ({home}, {home_mi:.0f} mi)")
    if cfg.relocate:
        reloc, reloc_mi = min(((c.name, miles_between(point, c.point)) for c in cfg.relocate),
                              key=lambda x: x[1])
        if reloc_mi <= cfg.relocate_radius_miles:
            return LocationTier(cfg.relocate_penalty, True, f"would-relocate ({reloc}, {reloc_mi:.0f} mi)")
    if home_mi <= cfg.outer_band_miles:
        return LocationTier(cfg.outer_band_penalty, True, f"near commute ({home}, {home_mi:.0f} mi)")
    return LocationTier(cfg.elsewhere_penalty, True, "elsewhere in US")


def home_bonus_for_point(point):
    """The home bonus for a resolved hybrid/onsite point inside
    home_bonus_radius_miles of the home metro, else 0 (GAP-07). Takes the
    point FIL-09 already resolved — no new lookup."""
    if miles_between(point, CONFIG.home.point) <= CONFIG.home_bonus_radius_miles:
        return CONFIG.home_bonus
    return 0


def _best_tier(points):
    # Several locations listed: the most favourable one counts.
    return min((tier_for_point(p) for p in points), key=lambda t: (-_rank(t.penalty), t.relo))


def _best_home_bonus(points):
    # Several locations listed: one near home earns the bonus, as with the tier.
    return max((home_bonus_for_point(p) for p in points), default=0)


def state_majority_penalties(config=None):
    """{state: (penalty, share)}: the penalty most of each state's population
    falls under, summing populated places (not county centroids) in the places
    file. Computed once per run (per config). The profile's
    state_tier_overrides are applied separately, in state_tier."""
    return _state_majority_penalties(config or CONFIG)


@lru_cache(maxsize=4)
def _state_majority_penalties(config):
    places, _ = _places()
    by_state = {}
    for plist in places.values():
        for p in plist:
            if p.population > 0:
                by_state.setdefault(p.state, Counter())[tier_for_point(_pt(p), config).penalty] += p.population
    result = {}
    for state, pops in by_state.items():
        penalty, pop = max(pops.items(), key=lambda kv: (kv[1], _rank(kv[0])))
        result[state] = (penalty, pop / sum(pops.values()))
    return result


def state_tier(abbr):
    """LocationTier for a state-only location."""
    penalty = CONFIG.state_override(abbr)
    if penalty is not None:
        return LocationTier(penalty, penalty != 0, f"state override ({abbr})")
    penalty, share = state_majority_penalties()[abbr]
    return LocationTier(penalty, penalty != 0, f"state majority ({abbr}, {share:.0%} of population)")


RESOLUTION_METHODS = ("posting", "scoring call", "most populous", "state majority", "unresolved")


def _clean_field(value):
    value = (value or "").strip() if isinstance(value, str) else ""
    return "" if value.lower() in ("null", "none", "unknown", "n/a") else value


def resolve_job_location(job):
    """(LocationTier, method, detail, home_bonus) for a hybrid/onsite job, trying
    in order: the posting's location string; the scoring call's city/state (job
    keys loc_city / loc_state); the most populous place of a bare town name; the
    state-majority tier of a state-only location. Otherwise unresolved.

    home_bonus (GAP-07) comes from the same resolved point as the tier. A
    state-only location resolves to no point and so never earns it."""
    location = (job.get("location") or "").strip()
    points = resolve_location(location)
    if points:
        return _best_tier(points), "posting", location, _best_home_bonus(points)

    city, state = _clean_field(job.get("loc_city")), _clean_field(job.get("loc_state"))
    if city or state:
        attempts = []
        if city and state:
            attempts.append(f"{city}, {state}")
        if state and location and not state_only(location):
            attempts.append(f"{location}, {state}")  # bare town + state
        if city and not state:
            attempts.append(city)
        for attempt in attempts:
            points = resolve_location(attempt)
            if points:
                return (_best_tier(points), "scoring call",
                        f"{location or '(blank)'} → {attempt}", _best_home_bonus(points))
        abbr = state_only(state)
        if abbr:
            return state_tier(abbr), "scoring call", f"{location or '(blank)'} → {abbr}", 0

    points = resolve_location(location, most_populous=True)
    if points:
        return _best_tier(points), "most populous", location, _best_home_bonus(points)
    abbr = state_only(location)
    if abbr:
        return state_tier(abbr), "state majority", location, 0
    return LocationTier(0, False, "unresolved"), "unresolved", location or "(blank)", 0


# How each scored hybrid/onsite job's location was resolved this run:
# method -> detail string -> job keys.
_resolutions = {}


def _job_key(job):
    return job.get("apply_link") or job.get("url") or f"{job.get('title')}|{job.get('company')}"


def location_resolution(job, record=False):
    """(LocationTier, method, home_bonus) for a job before exemptions. method is
    one of RESOLUTION_METHODS for hybrid/onsite jobs, else "international" or
    "not hybrid/onsite". Only hybrid/onsite jobs can earn the home bonus, so a
    remote role gets 0 however close to home it is (GAP-07).
    record: count this job toward the run-log resolution line."""
    location = job.get("location") or ""
    if is_international(location.lower()):
        return (LocationTier(CONFIG.outside_country, False, f"outside {CONFIG.country}"),
                "international", 0)
    if _model(job) not in _HYBRID_ONSITE:
        return LocationTier(0, False, "not hybrid/onsite"), "not hybrid/onsite", 0
    tier, method, detail, bonus = resolve_job_location(job)
    if record:
        _resolutions.setdefault(method, {}).setdefault(detail, set()).add(_job_key(job))
    return tier, method, bonus


def location_tier(job, record=False):
    """LocationTier for a job before exemptions. Hybrid/onsite only; remote,
    unknown work model and international carry no tier penalty.
    record: count this job toward the run-log resolution line (the scorer
    passes True, so only scored jobs are counted)."""
    return location_resolution(job, record=record)[0]


def needs_relo(job):
    """True when the job requires US relocation (either penalised tier)."""
    return location_tier(job).relo


def home_bonus(job):
    """The job's GAP-07 bonus: the profile's home_bonus for hybrid/in-office
    within home_bonus_radius_miles of the home metro, else 0."""
    return location_resolution(job)[2]


def location_penalty(job, tier=None):
    """Score penalty for a job that passes the filter.

    FIL-11: no exemptions. Every hybrid/onsite role outside the commutable
    band takes its tier's penalty, whatever the company and whatever the base
    score. The floor in apply_location_penalty is the only thing that softens
    it.
    """
    tier = tier or location_tier(job)
    # A dealbreaker tier is not a number; apply_location_tier records it as
    # job["dealbreaker"] and the stored penalty stays 0.
    return 0 if tier.penalty == DISQUALIFY else tier.penalty


# PUB-19: a dealbreaker role criterion applies only on evidence the code can
# check. The scoring call quotes the posting's sentence for it
# (dealbreaker_evidence); the quote must be in the description (whitespace and
# case aside), at least DEALBREAKER_QUOTE_MIN characters, and contain one of the
# criterion's evidence_terms (whole words). A hit that fails is not applied:
# it is recorded in job["dealbreaker_unverified"] and logged.
DEALBREAKER_QUOTE_MIN = 20


def _fold(text):
    return " ".join(str(text or "").split()).casefold()


def verify_dealbreaker(job, cid):
    """(applies, reason) for the dealbreaker criterion cid on this job:
    reason is "" when it applies, else why the evidence does not hold."""
    quote = (job.get("dealbreaker_evidence") or {}).get(cid)
    if not quote or not str(quote).strip():
        return False, "no sentence quoted"
    folded = _fold(quote)
    if len(folded) < DEALBREAKER_QUOTE_MIN:
        return False, f"quote under {DEALBREAKER_QUOTE_MIN} characters"
    if folded not in _fold(job.get("description")):
        return False, "quote not found in the posting"
    terms = dict(CONFIG.dealbreaker_terms).get(cid, ())
    if not any(re.search(r"(?<!\w)" + re.escape(_fold(t)) + r"(?!\w)", folded) for t in terms):
        return False, "quote has none of the evidence terms"
    return True, ""


def verified_dealbreaker(job):
    """The first dealbreaker criterion the scoring call reported (in profile
    order) whose evidence holds, or None. Every reported one whose evidence
    fails is recorded in job["dealbreaker_unverified"] ({id, reason, quote})
    and logged; a job with none of those carries no such field."""
    job.pop("dealbreaker_unverified", None)
    found, unverified = None, []
    for cid in job.get("criteria_hits") or ():
        if cid not in CONFIG.dealbreakers:
            continue
        ok, reason = verify_dealbreaker(job, cid)
        if ok:
            found = found or cid
            continue
        unverified.append({"id": cid, "reason": reason,
                           "quote": (job.get("dealbreaker_evidence") or {}).get(cid, "")})
        print(f"[Scorer] dealbreaker {cid} reported without verifiable evidence; ignored: {reason} "
              f"({job.get('title', '')} at {job.get('company', '')})")
    if unverified:
        job["dealbreaker_unverified"] = unverified
    return found


def apply_location_tier(job, base_score, record=True):
    """Set the FIL-09 location fields on a scored job and return its final score.

    base_score is the score before any location penalty. Sets relo, the tier
    penalty, the GAP-07 home bonus, the tier fields (base_score, location_method,
    location_reason) that mark a job as tiered by this code, and score.
    Used by scorer.score_jobs and by pending.retier_legacy for batches queued
    before FIL-09.

    PUB-09: the work-model outcome comes first (work_model_adjustment). Then
    the location tier and home bonus, then the comp adjustment, the function
    cap, and the floor and the cap. Any of the work model, the tier and comp
    may be a dealbreaker: the first one sets score 0 and job["dealbreaker"]
    and skips the rest. The scoring call's criteria_hits are named in the
    notes line ("Criteria: decision scope +3.").

    SCO-08: the comp adjustment (-2 / 0 / +1 on the top of the stated range)
    is applied here too, alongside the tier and before the floor and the cap,
    and named in the rationale the digest shows. The comp field is set to the
    full stated range so the digest never shows only its lower bound.

    SCO-09: a job whose function_fit is anything but leads_department is then
    capped at scoring.function_cap (function_capped), after every other
    adjustment, and the cap is named in the rationale."""
    tier, method, bonus = location_resolution(job, record=record)
    work_key = work_model_key(job)
    work = CONFIG.work_model_outcome(work_key)
    job["relo"] = tier.relo
    job["base_score"] = base_score
    job["location_method"] = method
    job["location_reason"] = tier.reason + (f", home bonus +{bonus}" if bonus else "")
    job["location_penalty"] = location_penalty(job, tier=tier)
    job["home_bonus"] = bonus
    job["work_model_adjustment"] = work if isinstance(work, int) else 0
    job.pop("dealbreaker", None)
    job.pop("dealbreaker_criterion", None)
    # PUB-03 renamed dc_bonus; a job queued before it is re-tiered here and
    # must not carry both.
    job.pop("dc_bonus", None)
    job["comp"] = display_range(job)
    adjustment, top = comp_adjustment(job)
    job["comp_adjustment"] = adjustment if base_score > 0 and adjustment != DISQUALIFY else 0
    # DEL-13: the notes go on their own line under the blurb, so the digest can
    # render them apart from it; the Sheet keeps the whole text.
    blurb, _ = split_rationale(job.get("rationale"))
    notes = [criteria_note(job.get("criteria_hits"))] if base_score > 0 else []

    # PUB-09: the first dealbreaker, in the order the outcomes apply. PUB-10:
    # a dealbreaker role criterion the scoring call reported comes first; the
    # code decides, whatever score the call returned.
    dealbreaker = None
    criterion = verified_dealbreaker(job)
    if criterion and not job.get("hard_block_industry"):
        dealbreaker = criterion_name(criterion)
    elif base_score > 0:
        if work == DISQUALIFY:
            dealbreaker = work_key
        elif tier.penalty == DISQUALIFY:
            dealbreaker = tier.reason
        elif adjustment == DISQUALIFY:
            dealbreaker = "below floor" if top is not None else "no stated pay"
    if dealbreaker:
        job["dealbreaker"] = dealbreaker
        if criterion:
            job["dealbreaker_criterion"] = criterion
            notes = [f"Dealbreaker: {dealbreaker}."]
        elif work == DISQUALIFY or tier.penalty == DISQUALIFY:
            job["location_reason"] = f"dealbreaker: {dealbreaker}"
            job["home_bonus"] = 0
            notes.append(f"Dealbreaker: {dealbreaker}.")
        else:
            notes.append(comp_note(adjustment, top))
        job["function_capped"] = False
        job["rationale"] = join_rationale(blurb, " ".join(n for n in notes if n))
        job["score"] = 0
        return 0

    if job["comp_adjustment"]:
        notes.append(comp_note(adjustment, top))
    if base_score > 0:
        notes.append(work_model_note(work_key, job["work_model_adjustment"]))
        notes.append(location_note(tier, bonus))
    score = apply_location_penalty(base_score, job["location_penalty"], bonus,
                                   job["comp_adjustment"], job["work_model_adjustment"])
    # SCO-09: after every other adjustment. A job scored before SCO-09 has no
    # function_fit and is never capped; a 0 stays 0.
    fit = function_fit_value(job.get("function_fit"))      # PUB-10: an old name reads as its new one
    if fit is not None:
        job["function_fit"] = fit
    # REV-08: a title-only LinkedIn posting's "unknown" is not capped: no
    # description was read, so nothing says the role is outside the department.
    title_only_unknown = fit == "unknown" and bool(job.get("title_only"))
    job["function_capped"] = (fit is not None and fit != FUNCTION_FIT_LEADERSHIP and score > 0
                              and not title_only_unknown)
    if job["function_capped"]:
        score = min(score, CONFIG.function_cap)
        notes.append(function_note(fit))
    job["rationale"] = join_rationale(blurb, " ".join(n for n in notes if n))
    job["score"] = score
    return job["score"]


def criteria_note(hits):
    """PUB-09: the notes-line entry naming the role criteria the scoring call
    found ("Criteria: decision scope +3, pure infosec -2."), or ""."""
    if not hits:
        return ""
    points = dict(CONFIG.criteria)
    hits = [h for h in hits if h not in CONFIG.dealbreakers]     # PUB-10: named in their own note
    if not hits:
        return ""
    named = [f"{criterion_name(h)} {points[h]:+d}" if h in points else criterion_name(h) for h in hits]
    return f"Criteria: {', '.join(named)}."


def criterion_name(cid):
    """A role criterion's id as the notes name it: "no_team" -> "no team"."""
    return cid.replace("_", " ")


def work_model_note(key, adjustment):
    """PUB-09: the note naming a work-model bonus or penalty, or ""."""
    return f"Work model: {key}: {adjustment:+d}." if adjustment else ""


def location_note(tier, bonus):
    """DEL-13: the note naming the relo penalty and the GAP-07 home bonus, or ""."""
    parts = []
    if tier.penalty and tier.relo:
        parts.append(f"Relo: {tier.reason}: {tier.penalty}.")
    elif tier.penalty:
        # PUB-09: a numeric location.outside_country (no relo tag)
        parts.append(f"Location: {tier.reason}: {tier.penalty:+d}.")
    if bonus:
        parts.append(f"Hybrid near {CONFIG.home.name}: +{bonus}.")
    return " ".join(parts)


# Notes from an earlier pass, so re-tiering never names an adjustment twice.
# "Hybrid near <home>" matches the pre-PUB-03 "Hybrid near DC" too.
LOCATION_NOTE_RE = re.compile(r"\s*(?:Relo: [^:\n]+: -\d+\.|Hybrid near [^:\n]+: \+\d+\."
                              r"|Location: [^:\n]+: [+-]\d+\.|Work model: \w+: [+-]\d+\.)")
# PUB-09: the criteria and dealbreaker notes.
CRITERIA_NOTE_RE = re.compile(r"\s*Criteria: [\w ,+-]+\.")
DEALBREAKER_NOTE_RE = re.compile(r"\s*Dealbreaker: [^\n]*?\.(?=\s|$)")


def split_rationale(text):
    """DEL-13: (blurb, notes) from a stored rationale. The notes are the
    code-appended comp, location and function-cap notes, wherever they sit;
    the blurb is everything else, its lines kept."""
    text = str(text or "")
    notes = []
    for pattern in (CRITERIA_NOTE_RE, DEALBREAKER_NOTE_RE, COMP_NOTE_RE, LOCATION_NOTE_RE, FUNCTION_NOTE_RE):
        notes += [m.group().strip() for m in pattern.finditer(text)]
        text = pattern.sub("", text)
    blurb = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return blurb, " ".join(notes)


def join_rationale(blurb, notes):
    """The stored rationale: the blurb, then the notes on a line of their own."""
    return "\n".join(part for part in (blurb, notes) if part)


_FUNCTION_LABELS = {"in_department": "in the target department but does not lead it",
                    "outside_department": "outside the target department"}


def function_note(fit):
    """The digest-rationale line naming the SCO-09 cap (PUB-10: generic wording)."""
    label = _FUNCTION_LABELS.get(function_fit_value(fit), "function unclassified")
    return f"Function: {label}: capped at {CONFIG.function_cap}."


# A note from an earlier pass, so re-tiering never names the cap twice; it also
# matches the pre-PUB-10 wording ("..., not IT leadership: capped at 4.").
FUNCTION_NOTE_RE = re.compile(r"\s*Function: [^:.]+(?:, not IT leadership)?: capped at \d+\.")


FIL09_TIER_FIELDS = ("base_score", "location_method")


def apply_location_penalty(score, penalty, bonus=0, comp=0, work=0):
    """Apply a location penalty, the GAP-07 home bonus, the SCO-08 comp
    adjustment and the PUB-09 work-model adjustment, with the floor and the
    cap: never below the profile's min_score_after_penalties, never above its
    max_score, and a score of 0 (hard block, international) stays 0."""
    if score <= 0 or not (penalty or bonus or comp or work):
        return score
    return min(CONFIG.max_score, max(CONFIG.score_floor, score + penalty + bonus + comp + work))


def passes_location_filter(job):
    """Return True if the job should proceed through the pipeline.
    Only locations outside location.country are dropped, and only while
    location.outside_country is "disqualify"; a numeric outside_country is
    applied after scoring instead. Tiers never drop."""
    return CONFIG.outside_country != DISQUALIFY or not is_international(_loc(job))


def log_location_resolutions():
    """One run-log line: how each scored hybrid/onsite location was resolved
    (posting, scoring call, most populous, state majority, unresolved), with
    counts, listing the strings behind every method except posting."""
    parts = []
    for method in RESOLUTION_METHODS:
        details = _resolutions.get(method, {})
        total = sum(len(keys) for keys in details.values())
        if method == "posting" or not details:
            parts.append(f"{method} {total}")
            continue
        listed = sorted(((d, len(k)) for d, k in details.items()), key=lambda x: (-x[1], x[0]))
        parts.append(f"{method} {total} {{" + ", ".join(f"{d!r}: {n}" for d, n in listed) + "}")
    print("[Location] Hybrid/onsite locations by resolution: " + " · ".join(parts))
    _resolutions.clear()  # a local `all` run logs fetch and digest separately


def reset_location_resolutions():
    _resolutions.clear()


# PUB-03: built last, once per run, from profile.yaml. profile_loader's
# validation resolves the metros through places.centre_for. Tests swap in another config with
# build_config(profile_dict) and monkeypatch; every cache keys on the config.
CONFIG = build_config(profile_loader.active())
