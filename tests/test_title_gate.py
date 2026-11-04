"""Title-gate coverage (PRD §3).

The gate is a cost control, not a quality filter: it must let every plausible
IT-leadership title through to the scorer and keep out titles that plainly are
not IT. The two lists below are the contract; add to them rather than
loosening a matcher. Any change to the gate must also pass the Sheet backtest
described in config.TITLE_GATED_SOURCES (zero historical score-7+ rows lost).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import profile_loader
from config import title_gate, title_matches
from sources.ats import _title_passes as ats_title_passes, title_passes_for

MUST_PASS = [
    "Head of IT",
    "Head of Information Technology",
    "Global Head of IT",
    "Head of Enterprise Technology",
    "Head of Corporate IT",
    "Head of Digital Workplace",
    "Head of Business Systems",
    "Head of Technology Operations",
    # widened 2026-09-11 — proven score-7+ titles from the Sheet
    "VP, Technology",
    "Head of Technology",
    "Vice President, Information Systems",
    "Chief Information Officer",
    "Deputy CIO",
    "CTO",
    "Chief Digital Officer",
    "Director, End User Services",
    "Vice President, People Technology",
    "Senior Director, IT Service Delivery",
    "Senior Vice President-Corporate Technology",
]

MUST_FAIL = [
    "Head of Marketing",
    "Head of Sales",
    "IT Support Manager",
    "CISO",
    # bare enterprise / digital / corporate never satisfy the function term
    "Head of Enterprise Sales",
    "Director, Enterprise Sales (AI & Digital Natives)",
    "Digital Communications Director",
    "Director, Corporate Development",
    "Area Director, Enterprise Solutions",
]

# The gate each gated source applies to a fetched title.
POST_FETCH_GATES = {
    "config.title_gate": title_gate,
    "config.title_matches": title_matches,
    "ats": ats_title_passes,
}


def _gates():
    """Built In and Wellfound import network deps at module load; skip them if
    those aren't installed rather than failing the whole file."""
    gates = dict(POST_FETCH_GATES)
    for name, mod in (("builtin", "sources.builtin"), ("wellfound", "sources.wellfound")):
        try:
            gates[name] = __import__(mod, fromlist=["_title_matches"])._title_matches
        except Exception as e:  # pragma: no cover - import-time dependency gap
            print(f"[skip] {name}: {e}")
    return gates


def test_it_leadership_titles_pass_every_gate():
    for name, gate in _gates().items():
        for title in MUST_PASS:
            assert gate(title), f"{name} dropped {title!r}"


def test_non_it_titles_fail_every_gate():
    for name, gate in _gates().items():
        for title in MUST_FAIL:
            assert not gate(title), f"{name} admitted {title!r}"


def test_exempt_sources_keep_their_proven_matches():
    # Watchlist is exempt (config.TITLE_GATED_SOURCES): the shared gate would
    # have dropped this score-8 role.
    title = "Senior Director, Enterprise Networking"
    assert "Watchlist" not in config.TITLE_GATED_SOURCES
    assert "LinkedIn" not in config.TITLE_GATED_SOURCES
    assert title_passes_for("Watchlist", title)
    assert not title_passes_for("ATS", title)


def test_head_of_gate_needs_a_function_term():
    assert title_matches("Head of Corporate Engineering")
    assert not title_matches("Head of People")
    assert not title_matches("")
