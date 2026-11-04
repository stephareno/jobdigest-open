# profile_loader.py — loads and validates profile.yaml (PUB-01)
#
# profile.yaml holds everything personal about the job search; this module
# reads it, checks it and returns a read-only tree: nested MappingProxyType for
# mappings, tuples for lists, read by key (profile["location"]["home"]).
# Nested mappings rather than frozen dataclasses: one SCHEMA table below drives
# both the structural checks and the error paths, so a new field is one line
# here instead of a new class that repeats the schema.
#
# Validation reports every problem at once, each with its field path
# ("location.relocate[3]: ..."), so the file can be fixed in one pass.
#
# `python main.py profile-check` runs check(). The pipeline reads the committed
# profile through active() (once per run): the prompts (PUB-02), the location
# tiers (PUB-03), the title gate, industries, pay, scores and model (PUB-04).
# PUB-05 switches the remaining source and delivery settings.

import functools
import os
import re
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

# A leaf module (standard library only). profile_loader never imports config,
# scorer, location or any other pipeline module: they all import it.
import company_lists
import places

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
# JOB_DIGEST_PROFILE points the whole pipeline at another profile file (a
# second job seeker, a test); unset, it is the committed profile.yaml.
DEFAULT_PATH = os.environ.get("JOB_DIGEST_PROFILE") or os.path.join(REPO_ROOT, "profile.yaml")
SOURCES_DIR = os.path.join(REPO_ROOT, "sources")
SCHEMA_VERSION = 1

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Modules in sources/ that are not job sources: they update existing Sheet rows.
NON_FETCH_MODULES = {"__init__", "applied_doc", "status_email"}
# candidate.pronouns -> the forms the prompts use (prompt_template.py).
# verb_s ends a present-tense verb: "she admires", "they admire".
PRONOUNS = {
    "she/her": {"subject": "she", "object": "her", "possessive": "her", "verb_s": "s"},
    "he/him": {"subject": "he", "object": "him", "possessive": "his", "verb_s": "s"},
    "they/them": {"subject": "they", "object": "them", "possessive": "their", "verb_s": ""},
}
# The pay floor USAJOBS can borrow instead of an integer of its own.
PAY_FLOOR_REF = "compensation.floor"
# PUB-09: a modifier's outcome is an integer (bonus or penalty) or this word:
# score 0, Disqualified tab, the reason recorded, never emailed.
DISQUALIFY = "disqualify"
WORK_MODELS = ("remote", "hybrid", "onsite")
# Values an optional field takes when the profile leaves it out (PUB-09),
# filled in by load() after validation.
DEFAULTS = {
    "location.country": "US",
    "location.outside_country": DISQUALIFY,
    "location.work_models": {"remote": 0, "hybrid": 0, "onsite": 0},
    "compensation.unstated": 0,
    "company_fit.enabled": True,
    "scoring.mission_values.enabled": True,
    "roles.function_fit_note": "",
    "roles.rationale_example": "Likely the top seat in its department (no one above it). Owns the function end to end: strategy, budget, team, vendors, core systems.",
    # PUB-10: the inbox readers are opt-in.
    "sources.status_tracking.enabled": False,
    "sources.applied_doc.enabled": False,
    "sources.company_lists": [],
    # PUB-12: the example profile's cap; a profile written before PUB-12 gets it too.
    "scoring.max_calls_per_run": 150,
}
# Sections the profile may leave out (their DEFAULTS apply).
OPTIONAL_SECTIONS = {"location.work_models", "sources.status_tracking", "sources.applied_doc"}
# Fields a release renamed: the old name is reported with its new one.
RENAMED = {"roles.target_function": "roles.target_department",           # PUB-10
           "roles.exec_titles_standalone": "roles.target_titles"}         # PUB-09
_CRITERION_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
CRITERION_POINTS = (-5, 5)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class ProfileError(ValueError):
    """The profile could not be loaded. .errors lists every problem found."""

    def __init__(self, path, errors):
        self.path = path
        self.errors = list(errors)
        lines = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"{path}: {len(self.errors)} problem(s)\n{lines}")


# --- Schema --------------------------------------------------------------------
# A dict is a section: every key is required and no other key is allowed.
# A Field is a value. sign: "bonus" must be > 0, "penalty" < 0, "positive" > 0.


class Field:
    def __init__(self, kind, sign=None, item=None, allow_empty=False, optional=False):
        self.kind = kind              # "str", "int", "bool", "list", "map", "int_or_str", "outcome", "criteria"
        self.sign = sign
        self.item = item              # list item / map value kind
        self.allow_empty = allow_empty
        self.optional = optional      # the key may be left out


STR = Field("str")
INT = Field("int")
BOOL = Field("bool")
POSITIVE = Field("int", sign="positive")
BONUS = Field("int", sign="bonus")
PENALTY = Field("int", sign="penalty")
STR_LIST = Field("list", item="str")
# A metro: a "City, ST" string, or {name, lat, lon} to pin its coordinates.
PLACE = Field("place")
PLACE_LIST = Field("list", item="place")
OPTIONAL_STR_LIST = Field("list", item="str", allow_empty=True)
# PUB-09: an integer or "disqualify". sign applies to the integer form.
OUTCOME = Field("outcome")
PENALTY_OUTCOME = Field("outcome", sign="penalty")

SCHEMA = {
    "version": INT,
    "candidate": {
        "display_name": STR,
        "short_name": STR,
        "pronouns": STR,
        "descriptor": STR,
        "headline": STR,
        "background": STR,
    },
    "roles": {
        "target_titles": STR_LIST,
        "target_department": STR,
        "function_fit_note": Field("str", optional=True, allow_empty=True),
        # PUB-11: the function-fit examples and the rationale's top-seat phrase
        "in_department_examples": STR_LIST,
        "outside_department_examples": STR_LIST,
        "top_seat_phrase": STR,
        # PUB-12: the example sentence of the rationale's role line (one line)
        "rationale_example": Field("str", optional=True),
        "seniority_terms": STR_LIST,
        "function_terms": STR_LIST,
        "function_terms_never_alone": OPTIONAL_STR_LIST,
        "title_gated_sources": OPTIONAL_STR_LIST,
        "watchlist_gate_terms": {
            "seniority": STR_LIST,
            "function": STR_LIST,
            "workday_search_terms": STR_LIST,
            "careers_page_seniority": STR_LIST,
            "careers_page_function": STR_LIST,
        },
    },
    "location": {
        "home": PLACE,
        "home_bonus_radius_miles": POSITIVE,
        "home_bonus": BONUS,
        "commutable": PLACE_LIST,
        "commutable_radius_miles": POSITIVE,
        "outer_band_miles": POSITIVE,
        "outer_band_penalty": PENALTY_OUTCOME,
        "relocate": Field("list", item="place", allow_empty=True),
        "relocate_radius_miles": POSITIVE,
        "relocate_penalty": PENALTY_OUTCOME,
        "elsewhere_penalty": PENALTY_OUTCOME,
        "state_tier_overrides": Field("map", item="int", allow_empty=True),
        "sizeable_place_population": POSITIVE,
        "country": Field("str", optional=True),
        "outside_country": Field("outcome", optional=True),
        "work_models": {"remote": Field("outcome", optional=True), "hybrid": Field("outcome", optional=True),
                        "onsite": Field("outcome", optional=True)},
    },
    "compensation": {
        "floor": POSITIVE,
        "floor_adjustment": PENALTY_OUTCOME,
        "stretch": POSITIVE,
        "stretch_adjustment": BONUS,
        "unstated": Field("outcome", optional=True),
    },
    "industries": {
        # tier entry (what the prompt names) -> name keywords the pre-filter matches
        "hard_block": Field("map", item="str_list"),
        "deprioritize": OPTIONAL_STR_LIST,
        "lowest_interest": OPTIONAL_STR_LIST,
        "neutral": OPTIONAL_STR_LIST,
        "lean_in": OPTIONAL_STR_LIST,
        "strong_yes": STR_LIST,
        "sector_order": OPTIONAL_STR_LIST,
        "vice_core_business": OPTIONAL_STR_LIST,
    },
    "company_fit": {
        "enabled": Field("bool", optional=True),
        # Required only while enabled (core_question, appeal_signals) or while
        # scoring.mission_values.enabled (operating_values, mission_flavors).
        "core_question": Field("str", optional=True),
        "appeal_signals": Field("list", item="str", optional=True),
        "operating_values": Field("list", item="str", optional=True),
        "mission_flavors": Field("list", item="str", optional=True),
        "exemplars": OPTIONAL_STR_LIST,
        "b2b_over_b2c": BOOL,
        "size_matters": BOOL,
    },
    "scoring": {
        "model": STR,
        "summary_model": Field("str", optional=True),
        "max_score": POSITIVE,
        "min_score_after_penalties": POSITIVE,
        "role_criteria": Field("criteria"),
        "mission_values": {"enabled": Field("bool", optional=True), "min": PENALTY, "max": BONUS,
                           "mission_alone": BONUS},
        "sector_adjustments": {
            "strong_yes": BONUS,
            "lean_in": BONUS,
            "deprioritize": PENALTY,
            "vice_core_business": PENALTY,
        },
        "thresholds": {"main_digest": POSITIVE, "worth_a_look": POSITIVE, "dream_company_main": POSITIVE},
        "function_cap": POSITIVE,
        "cool_company_wrong_role_cap": POSITIVE,
        # PUB-12: at most this many scoring calls per fetch run (main.fetch_run)
        "max_calls_per_run": Field("int", sign="positive", optional=True),
    },
    "dream_companies": {"file": STR},
    "sources": {
        "enabled": Field("map", item="bool"),
        # PUB-10: the two inbox readers, which update Sheet rows and fetch no postings.
        "status_tracking": {"enabled": Field("bool", optional=True)},
        "applied_doc": {"enabled": Field("bool", optional=True)},
        # PUB-11: company lists (company_lists.py): [{file, tier, filter}]
        "company_lists": Field("company_lists", optional=True),
        "linkedin_label": STR,
        "adzuna": {"queries": STR_LIST, "locations": Field("list", item="str", optional=True)},
        "jsearch": {"queries": STR_LIST},
        "builtin": {"search_urls": Field("list", item="str", optional=True), "title_queries": STR_LIST},
        "usajobs": {
            "series": STR,
            "hiring_paths": STR_LIST,
            "title_keywords": STR_LIST,
            "pay_floor_from": Field("int_or_str"),
        },
    },
    "delivery": {
        "email_to": STR,
        "timezone": STR,
        "digest_time_local": STR,
        "digest_days": STR_LIST,
        "weekly_summary_day": STR,
        "weekly_summary_time_local": Field("str", optional=True),
        "skip_empty_days": BOOL,
    },
}


# --- Structural checks ---------------------------------------------------------


def _type_ok(kind, value):
    # bool is a subclass of int in Python; a YAML `true` is never a number here.
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    raise AssertionError(kind)


def _type_name(value):
    return {bool: "boolean", int: "integer", float: "number", str: "string",
            list: "list", dict: "mapping", type(None): "empty"}.get(type(value), type(value).__name__)


def _place_name(value):
    return value.get("name") if isinstance(value, dict) else value


def _check_place(value, path, errors):
    """A metro: a non-blank string, or {name, lat, lon} with lat and lon
    given together as numbers in range."""
    if isinstance(value, str):
        if not value.strip():
            errors.append(f"{path}: must not be blank")
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: must be a \"City, ST\" string or a mapping with name, lat and lon, "
                      f"got {_type_name(value)} {value!r}")
        return
    for key in value:
        if key not in ("name", "lat", "lon"):
            errors.append(f"{path}.{key}: unknown field (a place takes name, lat and lon)")
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{path}.name: required, a non-blank string")
    if ("lat" in value) != ("lon" in value):
        errors.append(f"{path}: give lat and lon together, or neither")
    for key, bound in (("lat", 90), ("lon", 180)):
        if key in value:
            v = value[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append(f"{path}.{key}: must be a number, got {_type_name(v)} {v!r}")
            elif not -bound <= v <= bound:
                errors.append(f"{path}.{key}: must be between -{bound} and {bound}, got {v}")


def _check_criteria(value, path, errors):
    """scoring.role_criteria: a non-empty list of {id, points, criterion}, or
    {id, dealbreaker: true, criterion, evidence_terms} (PUB-10): points or
    dealbreaker, not both. PUB-19: a dealbreaker needs evidence_terms, a
    non-empty list of short phrases, one of which must appear in the sentence
    the scoring call quotes for it; points-based criteria take none."""
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list of criteria (id, points, criterion), got {_type_name(value)}")
        return
    if not value:
        errors.append(f"{path}: must list at least one criterion")
    low, high = CRITERION_POINTS
    seen = set()
    for i, item in enumerate(value):
        sub = f"{path}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{sub}: must be a mapping with id, points and criterion, got {_type_name(item)}")
            continue
        for key in item:
            if key not in ("id", "points", "criterion", "dealbreaker", "evidence_terms"):
                errors.append(f"{sub}.{key}: unknown field (a criterion takes id, points or dealbreaker, "
                              f"and criterion; a dealbreaker also takes evidence_terms)")
        cid = item.get("id")
        if not isinstance(cid, str) or not _CRITERION_ID_RE.fullmatch(cid):
            errors.append(f"{sub}.id: required, snake_case (lower-case letters, digits, underscores), got {cid!r}")
        elif cid in seen:
            errors.append(f"{sub}.id: duplicate id {cid!r}")
        else:
            seen.add(cid)
        points = item.get("points")
        deal = item.get("dealbreaker", False)
        if not _type_ok("bool", deal):
            errors.append(f"{sub}.dealbreaker: must be true or false, got {_type_name(deal)} {deal!r}")
        elif deal:
            if points not in (None, 0):
                errors.append(f"{sub}: has points ({points!r}) and dealbreaker: true; give one or the "
                              f"other (a dealbreaker is never scored: omit points or set it to 0)")
            terms = item.get("evidence_terms")
            if (not isinstance(terms, list) or not terms
                    or not all(isinstance(t, str) and t.strip() for t in terms)):
                errors.append(f"{sub}.evidence_terms: required for a dealbreaker, a non-empty list of short "
                              f"phrases a posting would use to say it (\"no direct reports\"); got {terms!r}")
        elif "evidence_terms" in item:
            errors.append(f"{sub}.evidence_terms: only a dealbreaker takes evidence_terms")
        elif points is None or not _type_ok("int", points):
            errors.append(f"{sub}.points: required, a non-zero integer from {low} to +{high}, got {points!r}")
        elif points == 0 or not low <= points <= high:
            errors.append(f"{sub}.points: must be a non-zero integer from {low} to +{high}, got {points}")
        text = item.get("criterion")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{sub}.criterion: required, one line the model can recognize in a posting")
        elif "\n" in text.strip():
            errors.append(f"{sub}.criterion: must be one line")


def _check_field(spec, value, path, errors):
    kind = spec.kind
    if kind == "place":
        _check_place(value, path, errors)
        return
    if kind == "criteria":
        _check_criteria(value, path, errors)
        return
    if kind == "company_lists":
        company_lists.check_entries(value, path, errors, places._STATES)
        return
    if kind == "outcome":
        if value == DISQUALIFY:
            return
        if not _type_ok("int", value):
            errors.append(f"{path}: must be an integer or \"{DISQUALIFY}\", got {_type_name(value)} {value!r}")
        elif spec.sign == "penalty" and value >= 0:
            errors.append(f"{path}: is a penalty and must be negative, got {value}")
        return
    if kind in ("str", "int", "bool"):
        if not _type_ok(kind, value):
            errors.append(f"{path}: must be {'an integer' if kind == 'int' else 'a ' + ('string' if kind == 'str' else 'boolean')}, "
                          f"got {_type_name(value)} {value!r}")
            return
        if kind == "str" and not value.strip() and not spec.allow_empty:
            errors.append(f"{path}: must not be blank")
        if spec.sign == "bonus" and value <= 0:
            errors.append(f"{path}: is a bonus and must be positive, got {value}")
        elif spec.sign == "penalty" and value >= 0:
            errors.append(f"{path}: is a penalty and must be negative, got {value}")
        elif spec.sign == "positive" and value <= 0:
            errors.append(f"{path}: must be positive, got {value}")
    elif kind == "int_or_str":
        if not (_type_ok("int", value) or _type_ok("str", value)):
            errors.append(f"{path}: must be an integer or a string, got {_type_name(value)} {value!r}")
    elif kind == "list":
        if not isinstance(value, list):
            errors.append(f"{path}: must be a list, got {_type_name(value)} {value!r}")
            return
        if not value and not spec.allow_empty:
            errors.append(f"{path}: must list at least one entry")
        seen = set()
        for i, item in enumerate(value):
            if spec.item == "place":
                before = len(errors)
                _check_place(item, f"{path}[{i}]", errors)
                if len(errors) > before:
                    continue
                key = _place_name(item).strip().casefold()
                if key in seen:
                    errors.append(f"{path}[{i}]: duplicate entry {_place_name(item)!r}")
                seen.add(key)
                continue
            if not _type_ok(spec.item, item) or (spec.item == "str" and not item.strip()):
                errors.append(f"{path}[{i}]: must be a non-blank string, got {_type_name(item)} {item!r}")
                continue
            key = item.strip().casefold()
            if key in seen:
                errors.append(f"{path}[{i}]: duplicate entry {item!r}")
            seen.add(key)
    elif kind == "map":
        if not isinstance(value, dict):
            errors.append(f"{path}: must be a mapping, got {_type_name(value)} {value!r}")
            return
        if not value and not spec.allow_empty:
            errors.append(f"{path}: must have at least one entry")
        for key, item in value.items():
            if spec.item == "str_list":
                _check_field(STR_LIST, item, f"{path}.{key}", errors)
            elif not _type_ok(spec.item, item):
                errors.append(f"{path}.{key}: must be {'a boolean' if spec.item == 'bool' else 'an integer'}, "
                              f"got {_type_name(item)} {item!r}")


def _check_section(schema, value, path, errors):
    if not isinstance(value, dict):
        errors.append(f"{path or 'profile'}: must be a mapping, got {_type_name(value)}")
        return
    for key, spec in schema.items():
        sub = f"{path}.{key}" if path else key
        if key not in value:
            if not getattr(spec, "optional", False) and sub not in OPTIONAL_SECTIONS:
                errors.append(f"{sub}: required field is missing")
        elif isinstance(spec, dict):
            _check_section(spec, value[key], sub, errors)
        else:
            _check_field(spec, value[key], sub, errors)
    for key in value:
        if key not in schema:
            sub = f"{path}.{key}" if path else str(key)
            if sub in RENAMED:
                errors.append(f"{sub}: renamed to {RENAMED[sub]}")
            else:
                errors.append(f"{sub}: unknown field (not in the profile schema; a typo?)")


# --- Cross-field checks --------------------------------------------------------
# Run after the structural pass, each only on fields that passed it, so one bad
# value is reported once.


def _bad_paths(errors):
    return {e.split(":", 1)[0] for e in errors}


def _get(data, path, bad):
    """The value at a dotted path, or None when it (or anything under or
    above it) already failed the structural pass."""
    for b in bad:
        if b == path or b.startswith(path + ".") or b.startswith(path + "[") or path.startswith(b + "."):
            return None
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _check_metros(data, bad, errors):
    names = []
    home = _get(data, "location.home", bad)
    if home is not None:
        names.append(("location.home", home))
    for field in ("commutable", "relocate"):
        for i, name in enumerate(_get(data, f"location.{field}", bad) or ()):
            names.append((f"location.{field}[{i}]", name))
    if not names:
        return
    # places.centre_for is the one resolution check (PUB-03); location builds
    # its config with the same function.
    for path, entry in names:
        bad_state = unknown_state_code(_place_name(entry))
        if bad_state:
            errors.append(f"{path}: {_place_name(entry)!r} does not resolve to a US place: "
                          f"{bad_state!r} is not a two-letter US state code")
            continue
        try:
            places.centre_for(entry)
        except places.PlaceError as e:
            errors.append(f"{path}: {e}")
    commutable = _get(data, "location.commutable", bad)
    if home is not None and commutable is not None:
        home_name = _place_name(home)
        if home_name.casefold() not in {_place_name(c).casefold() for c in commutable}:
            errors.append(f"location.home: {home_name!r} must also be listed in location.commutable")
    overrides = _get(data, "location.state_tier_overrides", bad)
    for state, penalty in (overrides or {}).items():
        if str(state) not in places._STATES:
            errors.append(f"location.state_tier_overrides.{state}: not a two-letter US state code")
        elif penalty > 0:
            errors.append(f"location.state_tier_overrides.{state}: must be 0 or a negative penalty, got {penalty}")


_CITY_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\.?\s*$")


def unknown_state_code(name):
    """The state code of a "City, ST" string when it is not a US state code
    ("ZZ" for "Nowhere, ZZ"), else None. places.centre_for alone ignores an
    unknown code and resolves the bare town (PUB-06 follow-up)."""
    m = _CITY_STATE_RE.search(str(name or ""))
    if m and m.group(1).upper() not in places._STATES:
        return m.group(1)
    return None


def _cross_checks(data, errors):
    bad = _bad_paths(errors)

    def get(path):
        return _get(data, path, bad)

    version = get("version")
    if version is not None and version != SCHEMA_VERSION:
        errors.append(f"version: this loader reads version {SCHEMA_VERSION}, got {version}")

    pronouns = get("candidate.pronouns")
    if pronouns is not None and pronouns not in PRONOUNS:
        errors.append(f"candidate.pronouns: must be one of {', '.join(PRONOUNS)}, got {pronouns!r}")

    # roles
    never_alone = get("roles.function_terms_never_alone")
    terms = get("roles.function_terms")
    if never_alone is not None and terms is not None:
        bare = {t.strip().casefold() for t in terms}
        for i, word in enumerate(never_alone):
            if word.strip().casefold() in bare:
                errors.append(f"roles.function_terms_never_alone[{i}]: {word!r} is also listed bare "
                              f"in roles.function_terms")

    # location
    _check_metros(data, bad, errors)
    commute, outer = get("location.commutable_radius_miles"), get("location.outer_band_miles")
    if commute is not None and outer is not None and outer <= commute:
        errors.append(f"location.outer_band_miles: must be greater than "
                      f"location.commutable_radius_miles ({commute}), got {outer}")
    country = get("location.country")
    covered = places.places_country()
    if country is not None and country.strip().upper() != covered:
        errors.append(f"location.country: {country!r} is not covered: {os.path.relpath(places.PLACES_FILE, REPO_ROOT)} "
                      f"holds {covered} places only. Another country needs its own places file "
                      f"(adapt tools/build_us_places.py to that country's GeoNames dump)")

    # industries: a name keyword belongs to one hard-block tier entry
    owner = {}
    for tier, keywords in (get("industries.hard_block") or {}).items():
        for word in keywords:
            other = owner.setdefault(word.strip().casefold(), tier)
            if other != tier:
                errors.append(f"industries.hard_block.{tier}: keyword {word!r} is also listed under {other!r}")

    # compensation
    floor, stretch = get("compensation.floor"), get("compensation.stretch")
    if floor is not None and stretch is not None and floor >= stretch:
        errors.append(f"compensation.floor: must be less than compensation.stretch ({stretch}), got {floor}")

    # scoring
    top, low = get("scoring.max_score"), get("scoring.min_score_after_penalties")
    if top is not None and low is not None and low >= top:
        errors.append(f"scoring.min_score_after_penalties: must be less than scoring.max_score ({top}), got {low}")
    main_t, look = get("scoring.thresholds.main_digest"), get("scoring.thresholds.worth_a_look")
    if main_t is not None and top is not None and main_t > top:
        errors.append(f"scoring.thresholds.main_digest: must be at most scoring.max_score ({top}), got {main_t}")
    if main_t is not None and look is not None and look >= main_t:
        errors.append(f"scoring.thresholds.worth_a_look: must be less than "
                      f"scoring.thresholds.main_digest ({main_t}), got {look}")
    dream = get("scoring.thresholds.dream_company_main")
    if dream is not None and main_t is not None and dream > main_t:
        errors.append(f"scoring.thresholds.dream_company_main: must be at most "
                      f"scoring.thresholds.main_digest ({main_t}), got {dream}")
    cap = get("scoring.function_cap")
    if cap is not None and look is not None and cap >= look:
        errors.append(f"scoring.function_cap: must be below scoring.thresholds.worth_a_look ({look}) "
                      f"so a capped role is never emailed, got {cap}")
    # PUB-09: the company-appeal and mission/values wording is required only
    # while its factor is on.
    if get("company_fit.enabled") is not False and "company_fit" in data and isinstance(data["company_fit"], dict):
        for key in ("core_question", "appeal_signals"):
            if key not in data["company_fit"]:
                errors.append(f"company_fit.{key}: required while company_fit.enabled is true")
    mission_on = get("scoring.mission_values.enabled") is not False
    if mission_on and isinstance(data.get("company_fit"), dict):
        for key in ("operating_values", "mission_flavors"):
            if key not in data["company_fit"]:
                errors.append(f"company_fit.{key}: required while scoring.mission_values.enabled is true")
    for key in ("appeal_signals", "operating_values", "mission_flavors"):
        listed = get(f"company_fit.{key}")
        if listed is not None and not listed:
            errors.append(f"company_fit.{key}: must list at least one entry")
    alone, mv_max = get("scoring.mission_values.mission_alone"), get("scoring.mission_values.max")
    if alone is not None and mv_max is not None and alone > mv_max:
        errors.append(f"scoring.mission_values.mission_alone: must be at most "
                      f"scoring.mission_values.max ({mv_max}), got {alone}")

    # dream companies
    dream_file = get("dream_companies.file")
    if dream_file is not None:
        full = dream_file if os.path.isabs(dream_file) else os.path.join(REPO_ROOT, dream_file)
        if not os.path.isfile(full):
            errors.append(f"dream_companies.file: {dream_file!r} does not exist")
        else:
            with open(full) as fh:
                if not any(line.strip() for line in fh):
                    errors.append(f"dream_companies.file: {dream_file!r} lists no companies")

    # sources
    enabled = get("sources.enabled")
    if enabled is not None:
        modules = {f[:-3] for f in os.listdir(SOURCES_DIR) if f.endswith(".py")} - NON_FETCH_MODULES
        for name in enabled:
            if name not in modules:
                errors.append(f"sources.enabled.{name}: no such source (no sources/{name}.py); "
                              f"known: {', '.join(sorted(modules))}")
        for name in sorted(modules - set(enabled)):
            errors.append(f"sources.enabled.{name}: missing; every source in sources/ needs true or false")
        gated = get("roles.title_gated_sources")
        for i, name in enumerate(gated or ()):
            if name not in enabled:
                errors.append(f"roles.title_gated_sources[{i}]: {name!r} is not a source in sources.enabled")
    for i, url in enumerate(get("sources.builtin.search_urls") or ()):
        if not url.startswith("https://"):
            errors.append(f"sources.builtin.search_urls[{i}]: must be an https:// URL, got {url!r}")
    series = get("sources.usajobs.series")
    if series is not None and not re.fullmatch(r"\d{4}", series):
        errors.append(f"sources.usajobs.series: must be a four-digit OPM job series such as \"2210\", got {series!r}")
    pay_floor = get("sources.usajobs.pay_floor_from")
    if isinstance(pay_floor, str) and pay_floor != PAY_FLOOR_REF:
        errors.append(f"sources.usajobs.pay_floor_from: must be {PAY_FLOOR_REF!r} or an annual USD integer, "
                      f"got {pay_floor!r}")
    elif isinstance(pay_floor, int) and pay_floor <= 0:
        errors.append(f"sources.usajobs.pay_floor_from: must be positive, got {pay_floor}")

    # delivery
    email = get("delivery.email_to")
    if email is not None and not _EMAIL_RE.fullmatch(email.strip()):
        errors.append(f"delivery.email_to: not an email address: {email!r}")
    tz = get("delivery.timezone")
    if tz is not None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"delivery.timezone: not an IANA time zone (such as \"America/New_York\"): {tz!r}")
    at = get("delivery.digest_time_local")
    if at is not None and not _TIME_RE.fullmatch(at):
        errors.append(f"delivery.digest_time_local: must be 24-hour HH:MM such as \"08:00\", got {at!r}")
    for i, day in enumerate(get("delivery.digest_days") or ()):
        if day not in WEEKDAYS:
            errors.append(f"delivery.digest_days[{i}]: not a weekday name ({', '.join(WEEKDAYS)}): {day!r}")
    weekly_at = get("delivery.weekly_summary_time_local")
    if weekly_at is not None and not _TIME_RE.fullmatch(str(weekly_at)):
        errors.append(f"delivery.weekly_summary_time_local: must be 24-hour HH:MM such as \"08:00\", "
                      f"got {weekly_at!r}")
    weekly = get("delivery.weekly_summary_day")
    if weekly is not None and weekly not in WEEKDAYS:
        errors.append(f"delivery.weekly_summary_day: not a weekday name ({', '.join(WEEKDAYS)}): {weekly!r}")


# --- Public API ----------------------------------------------------------------


def validate(data):
    """Every problem with a parsed profile, as "<field path>: <what is wrong>"
    strings. Empty when the profile is valid."""
    errors = []
    _check_section(SCHEMA, data, "", errors)
    if isinstance(data, dict):
        _cross_checks(data, errors)
    return errors


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def read(path=DEFAULT_PATH):
    """The parsed YAML as plain dicts and lists, unvalidated."""
    try:
        with open(path) as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        raise ProfileError(path, ["file not found"]) from None
    except yaml.YAMLError as e:
        raise ProfileError(path, [f"not valid YAML: {e}"]) from None


def _thaw(value):
    if isinstance(value, (dict, MappingProxyType)):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def with_defaults(data):
    """A plain copy of a validated profile (a parsed file or a loaded tree)
    with every DEFAULTS value filled in where it is left out (PUB-09)."""
    data = _thaw(data)
    for path, default in DEFAULTS.items():
        *parents, leaf = path.split(".")
        node = data
        for part in parents:
            node = node.setdefault(part, {})
        if isinstance(default, dict):
            node[leaf] = {**default, **(node.get(leaf) or {})}
        else:
            node.setdefault(leaf, default)
    return data


def load(path=DEFAULT_PATH):
    """The validated profile as a read-only tree, DEFAULTS filled in. Raises
    ProfileError listing every problem when the file is missing, unparseable
    or invalid."""
    data = read(path)
    errors = validate(data)
    if errors:
        raise ProfileError(path, errors)
    return _freeze(with_defaults(data))


@functools.cache
def active():
    """The committed profile (profile.yaml), loaded and validated once per run.
    Every pipeline module reads its personal values from here; a test that
    needs another profile builds the module's config from a dict instead."""
    return load(DEFAULT_PATH)


def dream_companies(profile=None):
    """The dream-company names, in file order, from the file the profile's
    dream_companies.file names (one name per line, blanks skipped), then
    (PUB-11) the names in the dream-tier company lists not already there."""
    p = profile or active()
    path = p["dream_companies"]["file"]
    full = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
    with open(full) as fh:
        names = [line.strip() for line in fh if line.strip()]
    have = {n.casefold() for n in names}
    for name in company_lists.dream_names(p["sources"].get("company_lists") or ()):
        if name.casefold() not in have:
            have.add(name.casefold())
            names.append(name)
    return names


def local_tz(profile=None):
    """delivery.timezone as a ZoneInfo: the zone "today", the digest date,
    the fresh flag and the schedule are read in."""
    return ZoneInfo((profile or active())["delivery"]["timezone"])


def local_today(profile=None, now=None):
    """Today's date in delivery.timezone (now: an aware datetime, default the
    current time)."""
    from datetime import datetime, timezone
    return (now or datetime.now(timezone.utc)).astimezone(local_tz(profile)).date()


def place_city(entry):
    """The city part of a metro entry: "Denver" for "Denver, CO" or for
    {name: "Denver, CO", lat, lon}."""
    return _place_name(entry).split(",")[0].strip()


def usajobs_pay_floor(profile=None):
    """The annual USD floor USAJOBS postings must reach at the top of their
    range: sources.usajobs.pay_floor_from, where "compensation.floor" means
    the comp floor."""
    p = profile or active()
    value = p["sources"]["usajobs"]["pay_floor_from"]
    return p["compensation"]["floor"] if value == PAY_FLOOR_REF else value


def _outcome(value):
    return value if value == DISQUALIFY else f"{value:+d}" if value else "0"


def summary(profile):
    """A short human summary of a loaded profile, one fact per line."""
    loc, comp, delivery = profile["location"], profile["compensation"], profile["delivery"]
    enabled = profile["sources"]["enabled"]
    on = [name for name, flag in enabled.items() if flag]
    off = [name for name, flag in enabled.items() if not flag]
    days = ", ".join(d[:3] for d in delivery["digest_days"])
    return "\n".join([
        f"Candidate:   {profile['candidate']['display_name']} — {profile['candidate']['headline']}",
        f"Home metro:  {_place_name(loc['home'])} (+{loc['home_bonus']} within {loc['home_bonus_radius_miles']} mi)",
        f"Commutable:  {len(loc['commutable'])} metros within {loc['commutable_radius_miles']} mi",
        f"Relocate:    {len(loc['relocate'])} metros within {loc['relocate_radius_miles']} mi "
        f"({_outcome(loc['relocate_penalty'])}); elsewhere {_outcome(loc['elsewhere_penalty'])}",
        f"Criteria:    " + ", ".join(f"{c['id']} dealbreaker" if c.get("dealbreaker") else f"{c['id']} {c['points']:+d}"
                                     for c in profile["scoring"]["role_criteria"]),
        f"Modifiers:   work models " + ", ".join(f"{k} {_outcome(v)}" for k, v in loc["work_models"].items())
        + f"; outside {loc['country']} {_outcome(loc['outside_country'])}",
        f"Comp:        floor ${comp['floor']:,} ({_outcome(comp['floor_adjustment'])}), "
        f"stretch ${comp['stretch']:,} (+{comp['stretch_adjustment']}), no stated pay {_outcome(comp['unstated'])}",
        f"Sources:     {len(on)} enabled: {', '.join(on)}" + (f"; off: {', '.join(off)}" if off else ""),
        f"Readers:     status tracking {'on' if profile['sources']['status_tracking']['enabled'] else 'off'}, "
        f"applied doc {'on' if profile['sources']['applied_doc']['enabled'] else 'off'}",
        f"Delivery:    by {delivery['digest_time_local']} {delivery['timezone']} on {days} "
        f"to {delivery['email_to']}; weekly summary {delivery['weekly_summary_day']} "
        f"{delivery.get('weekly_summary_time_local') or '08:00'}",
    ])


def check(path=DEFAULT_PATH):
    """`python main.py profile-check`: load, validate, print. 0 if valid, 1 if not."""
    try:
        profile = load(path)
    except ProfileError as e:
        print(f"[Profile] INVALID — {e}")
        return 1
    print(f"[Profile] OK — {path}")
    print(summary(profile))
    return 0
