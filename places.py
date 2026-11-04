# places.py — the offline US places file and the location-string resolver
#
# Turns a location string ("Austin, TX", "Greater Boston Area", "Hanover,
# Anne Arundel County") into (lat, lon) points from data/us_places.tsv.gz
# (GeoNames US places and counties, built by tools/build_us_places.py), read
# once per run, no network. Also resolves the profile's metros (centre_for).
#
# Split out of location.py in PUB-04 so profile_loader can validate metros
# without importing a pipeline module: this module imports only the standard
# library. location.py builds the tiers on top of it and re-exports these
# names, so location.resolve_location, location._places and the rest are
# unchanged for their callers.

import gzip
import math
import os
import re
from collections import namedtuple
from collections.abc import Mapping
from functools import lru_cache

# A metro: its profile name and the (lat, lon) it resolved to.
Centre = namedtuple("Centre", "name point")

# Same-named places closer than this count as one place when nothing else
# (state, county) disambiguates a bare town name.
SAME_PLACE_MILES = 25

PLACES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "us_places.tsv.gz")
# PUB-09: the country the places file covers, one ISO code on the first line
# of this sidecar (written by tools/build_us_places.py). location.country must
# name it; the places and the state codes below are that country's.
PLACES_COUNTRY_FILE = PLACES_FILE + ".country"


def places_country():
    """The ISO country code the places file covers ("US")."""
    with open(PLACES_COUNTRY_FILE) as fh:
        return fh.readline().strip().upper()

# --- Places file ---------------------------------------------------------------

_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "district of columbia", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming",
}
_STATE_BY_NAME = {name: abbr for abbr, name in _STATES.items()}

_COUNTRY_TOKENS = {"us", "usa", "united states", "united states of america", "america"}
_NOISE_TOKENS = {"hybrid", "remote", "onsite", "on-site", "on site", "in-office",
                 "in office", "office", "hq", "headquarters"}
_COUNTY_SUFFIXES = (" county", " parish", " borough", " census area")
_DC_TOKENS = {"dc", "district of columbia", "washington dc", "washington district of columbia"}

# Names postings use that GeoNames stores differently: name -> (place, state).
_NAME_ALIASES = {
    "new york": ("new york city", "NY"),
    "nyc": ("new york city", "NY"),
    "san francisco bay": ("san francisco", "CA"),  # LinkedIn "San Francisco Bay Area"
}

_WORD_EXPANSIONS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}


def _norm(text):
    text = text.lower().replace(".", "").replace("’", "'")
    text = re.sub(r"[^a-z0-9' -]", " ", text)
    words = [_WORD_EXPANSIONS.get(w, w) for w in text.split()]
    return " ".join(words)


Place = namedtuple("Place", "name state county lat lon population")


@lru_cache(maxsize=1)
def _places():
    """Load the places file once per run: (places by name, counties by name)."""
    if not os.path.exists(PLACES_FILE):
        raise FileNotFoundError(
            f"{PLACES_FILE} is missing — rebuild it with tools/build_us_places.py")
    places, counties = {}, {}
    with gzip.open(PLACES_FILE, "rt", encoding="utf-8") as fh:
        next(fh)  # header
        for line in fh:
            kind, name, state, county, lat, lon, pop = line.rstrip("\n").split("\t")
            p = Place(name, state, _norm(county), float(lat), float(lon), int(pop or 0))
            (places if kind == "P" else counties).setdefault(_norm(name), []).append(p)
    return places, counties


def miles_between(a, b):
    """Great-circle distance in miles between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _pt(place):
    return (place.lat, place.lon)


def _most_populous(cands):
    return max(cands, key=lambda p: p.population)


def _one_place(cands):
    """The single place a candidate list means, or None if they are far apart."""
    best = _most_populous(cands)
    if all(miles_between(_pt(best), _pt(c)) <= SAME_PLACE_MILES for c in cands):
        return best
    return None


# --- Resolver ------------------------------------------------------------------

def _split_trailing_state(token):
    """'Austin TX' -> ('Austin', 'TX'); 'Arlington Virginia' -> ('Arlington', 'VA')."""
    words = token.split()
    if len(words) >= 2 and words[-1].isupper() and words[-1] in _STATES:
        return " ".join(words[:-1]), words[-1]
    lowered = [w.lower() for w in words]
    for n in (2, 1):
        if len(words) > n and " ".join(lowered[-n:]) in _STATE_BY_NAME:
            return " ".join(words[:-n]), _STATE_BY_NAME[" ".join(lowered[-n:])]
    return token, None


def _resolve_one(text, metro_phrase=False, most_populous=False, sizeable_population=None, rank=None):
    """Resolve one location phrase to (lat, lon), or None.
    most_populous: the last-resort fallback. A bare town name shared by
    far-apart places resolves to the one `rank` (point -> sort key, lowest
    first) prefers among those of sizeable_population+ people (else the
    largest place), and "Washington" alone means DC."""
    raw_tokens = [t.strip() for t in text.replace(".", "").split(",") if t.strip()]
    if not raw_tokens:
        return None

    # Tokens as (normalised, original-case) with countries and work-model words dropped.
    tokens = []
    for t in raw_tokens:
        n = _norm(t)
        if n and n not in _COUNTRY_TOKENS and n not in _NOISE_TOKENS:
            tokens.append((n, t))
    if not tokens:
        return None

    places, counties = _places()

    def dc_point():
        return _pt(_most_populous([p for p in places["washington"] if p.state == "DC"]))

    # "Washington" with no state or country token: DC, not Washington State.
    if most_populous and len(raw_tokens) == 1 and _norm(raw_tokens[0]) == "washington":
        return dc_point()

    # Washington, D.C. in any spelling — DC is small enough to be one point.
    if any(n in _DC_TOKENS or n.endswith(" dc") for n, _ in tokens):
        return dc_point()

    name = state = county = None
    for i, (n, orig) in enumerate(tokens):
        if n.endswith(_COUNTY_SUFFIXES):
            county = county or n
            continue
        abbr = orig.upper() if orig.upper() in _STATES and len(orig) == 2 else _STATE_BY_NAME.get(n)
        later_state = any(
            (o.upper() in _STATES and len(o) == 2) or m in _STATE_BY_NAME
            for m, o in tokens[i + 1:])
        if abbr and not (name is None and later_state):
            state = state or abbr
            continue
        if name is None:
            base, trailing = _split_trailing_state(orig)
            name = _norm(base)
            state = state or trailing

    if name in _NAME_ALIASES and state in (None, _NAME_ALIASES[name][1]):
        name, state = _NAME_ALIASES[name][0], _NAME_ALIASES[name][1]

    county_pts = []
    if county:
        county_pts = [c for c in counties.get(county, []) if not state or c.state == state]

    if name:
        cands = list(places.get(name, []))
        if state:
            cands = [c for c in cands if c.state == state]
        if county:
            # GeoNames assigns a place to one county; postings may name another
            # it straddles (Hanover MD is Howard in GeoNames, Anne Arundel in
            # Adzuna), so a place near the named county counts too.
            cands = [c for c in cands if c.county == county or any(
                miles_between(_pt(c), _pt(cp)) <= SAME_PLACE_MILES for cp in county_pts)]
        if cands:
            if state or metro_phrase:
                return _pt(_most_populous(cands))
            one = _one_place(cands)
            if one:
                return _pt(one)
            if most_populous:
                sizeable = [c for c in cands if c.population >= sizeable_population]
                if sizeable:
                    best = min(sizeable, key=lambda c: (*rank(_pt(c)), -c.population))
                    return _pt(best)
                return _pt(_most_populous(cands))
            return None

    # No usable town: fall back to the county centroid.
    if county_pts:
        one = _one_place(county_pts)
        return _pt(one) if one else None
    return None


_METRO_RE = re.compile(r"\b(greater|metropolitan|metro|metroplex|area)\b", re.I)
# " or " separates locations only with whitespace on both sides and not straight
# after a comma, so a trailing ", OR" (Oregon) is always the state.
_ALT_SPLIT_RE = re.compile(
    r"\s*(?:;|\||/|\n|\s-\s|\s–\s)\s*|(?<![,\s])\s+or\s+", re.I)


def resolve_points(location, most_populous=False, sizeable_population=None, rank=None):
    """All US points a location string names, as a tuple of (lat, lon).

    Empty tuple when nothing resolves ("United States", a bare state, a town
    name shared by far-apart places with nothing to disambiguate it).
    Multi-location strings ("Austin, TX; Denver, CO") return one point each.
    most_populous, sizeable_population and rank: see _resolve_one. Uncached:
    location.resolve_location caches it per location config.
    """
    if not location:
        return ()
    # "Austin, TX (Hybrid)" / "Hybrid (Washington, D.C.)": brackets are separators
    text = re.sub(r"[()\[\]]", ",", location)
    text = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", text)
    points = []
    for alt in _ALT_SPLIT_RE.split(text):
        if not alt or not alt.strip():
            continue
        metro = bool(_METRO_RE.search(alt))
        if metro:
            alt = _METRO_RE.sub(" ", alt)
            parts = [alt] + (alt.split("-") if "-" in alt else [])
        else:
            parts = [alt]
        for part in parts:
            pt = _resolve_one(part, metro_phrase=metro, most_populous=most_populous,
                               sizeable_population=sizeable_population, rank=rank)
            if pt:
                points.append(pt)
                break
    return tuple(points)


def state_only(location):
    """The state abbreviation when a location names a state and nothing else
    ("Maryland", ", Wyoming", "Texas, US"), else None."""
    if not location:
        return None
    text = re.sub(r"[()\[\]]", ",", location)
    states = set()
    for alt in _ALT_SPLIT_RE.split(text):
        for t in (alt or "").replace(".", "").split(","):
            t = t.strip()
            n = _norm(t)
            if not n or n in _COUNTRY_TOKENS or n in _NOISE_TOKENS:
                continue
            abbr = t.upper() if len(t) == 2 and t.upper() in _STATES else _STATE_BY_NAME.get(n)
            if not abbr:
                return None
            states.add(abbr)
    return states.pop() if len(states) == 1 else None


# --- Profile metros ----------------------------------------------------------

class PlaceError(ValueError):
    """A profile metro that does not resolve to exactly one place."""


def centre_for(entry):
    """The Centre for a profile metro: a "City, ST" string resolved against the
    places file, or a {name, lat, lon} mapping whose coordinates win. Raises
    PlaceError when a name resolves to no place or to several. This is the one
    resolution check: profile_loader validates with it, location builds its
    config with it."""
    if isinstance(entry, Mapping):
        name = entry["name"]
        if "lat" in entry and "lon" in entry:
            return Centre(name, (float(entry["lat"]), float(entry["lon"])))
    else:
        name = entry
    points = resolve_points(name)
    if not points:
        raise PlaceError(f"{name!r} does not resolve to a US place in data/us_places.tsv.gz "
                         f"(write it as \"City, ST\", or give lat and lon)")
    if len(points) > 1:
        raise PlaceError(f"{name!r} resolves to {len(points)} places; name exactly one")
    return Centre(name, points[0])
