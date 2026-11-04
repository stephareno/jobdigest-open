# comp.py — stated compensation and the comp adjustment (SCO-08)
#
# Compensation is judged on the top of the posted range, against the
# profile's compensation section (PUB-04; committed values in brackets):
#   below compensation.floor ($180,000)         compensation.floor_adjustment (-2)
#   compensation.stretch ($240,000) and above   compensation.stretch_adjustment (+1)
#   between                                     0
#   no stated compensation                      compensation.unstated (0)
# The scoring prompt never sees a comp rule; this module owns all of it.
# PUB-09: floor_adjustment and unstated may be "disqualify" (a dealbreaker:
# location.apply_location_tier scores the role 0).
#
# "Stated" means the posting says it: the source's comp field (unless the
# source estimated it -- Adzuna's salary_is_predicted) or a pay figure in the
# description. Nothing is inferred: no hourly-to-annual conversion, no market
# estimate, no guess from the title. A minimum with no top ("$150,000+") has no
# stated top, so it takes the unstated outcome.
#
# Applied by location.apply_location_tier, with the location tier and the home
# bonus, before the score floor and the cap (scoring.min_score_after_penalties,
# scoring.max_score).

import re

import profile_loader
from profile_loader import DISQUALIFY

# profile["compensation"]: floor, floor_adjustment, stretch, stretch_adjustment.
# Every function below takes another one as `comp` (tests, other profiles).
COMP = profile_loader.active()["compensation"]

# A stated annual figure lies in this band. Below it is hourly or a typo; above
# it is a budget or a funding round, not a salary.
MIN_ANNUAL = 30_000
MAX_ANNUAL = 2_000_000

_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?(?![\w.]*\s*(?:[mMbB]\b|million|billion|mm\b))"
_AMOUNT = r"\$\s?" + _NUMBER
# "$150,000 - $180,000", "$150K–$180K", "$150-180K", "$150,000 to 180,000"
_RANGE_RE = re.compile(_AMOUNT + r"\s*(?:-|–|—|to)\s*(?:USD\s*)?\$?\s?" + _NUMBER)
# A single figure counts only next to a pay word, so "$50M ARR" or "$2B in
# assets under management" never reads as a salary.
_PAY_WORDS = r"(?:salary|compensation|pay|base|range|ote|annual|wage)"
_SINGLE_RE = re.compile(_PAY_WORDS + r"[^$\n]{0,40}" + _AMOUNT, re.I)
_HOURLY_RE = re.compile(r"^\s*(?:/|per\s+|an\s+|a\s+)?\s*(?:hr|hour)\b", re.I)


def _value(number, k):
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


def _annual(value):
    return MIN_ANNUAL <= value <= MAX_ANNUAL


def stated_ranges(text):
    """Every stated annual pay range in `text` as (low, high); a lone pay figure is (x, x)."""
    if not text:
        return []
    ranges, taken = [], []
    for m in _RANGE_RE.finditer(text):
        if _HOURLY_RE.match(text[m.end():m.end() + 12]):
            continue
        # A K on one side applies to both: "$150-180K", "$150K-180".
        k = m.group(2) or m.group(4)
        low = _value(m.group(1), m.group(2) or (k if "," not in m.group(1) else None))
        high = _value(m.group(3), m.group(4) or (k if "," not in m.group(3) else None))
        if _annual(low) and _annual(high):
            ranges.append((min(low, high), max(low, high)))
            taken.append(m.span())
    for m in _SINGLE_RE.finditer(text):
        start = m.start(1) - 1
        if any(a <= start < b for a, b in taken):
            continue
        tail = text[m.end():m.end() + 12]
        if _HOURLY_RE.match(tail) or tail.lstrip().startswith("+"):
            continue          # hourly, or a minimum with no stated top
        value = _value(m.group(1), m.group(2))
        if _annual(value):
            ranges.append((value, value))
    return ranges


def stated_tops(text):
    """Top of every stated annual pay range or pay figure in `text`."""
    return [high for _, high in stated_ranges(text)]


def _job_ranges(job):
    ranges = []
    if not job.get("comp_is_predicted"):
        ranges += stated_ranges(job.get("comp") or "")
    ranges += stated_ranges(job.get("description") or "")
    return ranges


def stated_comp_top(job):
    """The highest stated annual pay figure on the posting, or None if none is stated.

    The highest across every range found, so a posting listing several
    geographic ranges is judged on its best one.
    """
    ranges = _job_ranges(job)
    return max(high for _, high in ranges) if ranges else None


def comp_adjustment(job, comp=None):
    """(adjustment, stated top or None): floor_adjustment below the floor,
    stretch_adjustment at the stretch or above, unstated when no top is
    stated, else 0. An adjustment may be DISQUALIFY (PUB-09)."""
    c = comp or COMP
    top = stated_comp_top(job)
    if top is None:
        return c.get("unstated", 0), None
    if top < c["floor"]:
        return c["floor_adjustment"], top
    if top >= c["stretch"]:
        return c["stretch_adjustment"], top
    return 0, top


def comp_note(adjustment, top, comp=None):
    """The digest-rationale line naming the adjustment, or "" for none."""
    c = comp or COMP
    if adjustment == DISQUALIFY:
        if top is None:
            return "Dealbreaker: no stated pay."
        return f"Dealbreaker: below floor (comp tops out at {_k(top)}, below {_k(c['floor'])})."
    if top is None:
        return f"No stated pay: {adjustment:+d}." if adjustment else ""
    if adjustment < 0:
        return f"Comp tops out at {_k(top)}, below {_k(c['floor'])}: {adjustment}."
    if adjustment > 0:
        return f"Comp tops out at {_k(top)}, {_k(c['stretch'])}+: +{adjustment}."
    return ""


# A note from an earlier pass, so re-tiering never names the adjustment twice.
COMP_NOTE_RE = re.compile(r"\s*(?:Comp tops out at \$[\d,.]+K, (?:below \$[\d,.]+K: -\d+|\$[\d,.]+K\+: \+\d+)\."
                         r"|No stated pay: [+-]\d+\.)")


def _k(value):
    return f"${value / 1000:,.0f}K"


def _money(value):
    return f"${value:,.0f}"


def display_range(job):
    """The full stated range for the digest and Sheet, e.g. "$146,000 – $258,000".

    The source's comp field is kept when it already states a range with a top
    (Adzuna, JSearch). Otherwise -- empty, a minimum only, or an Adzuna
    estimate -- the posting's own stated pay is shown: lowest bottom to highest
    top across every range it lists. Returns the comp field unchanged when the
    posting states nothing.
    """
    current = job.get("comp") or ""
    if not job.get("comp_is_predicted") and any(lo != hi for lo, hi in stated_ranges(current)):
        return current
    ranges = _job_ranges(job)
    if not ranges:
        return current
    low, high = min(lo for lo, _ in ranges), max(hi for _, hi in ranges)
    return _money(high) if low == high else f"{_money(low)} – {_money(high)}"
