# FIL-10: the second dedup key — normalised company + title + location.
#
# The key's whole job is to decide when two postings are the same opening, so
# these tests are split into "must collide" (the same job reaching us twice)
# and "must not collide" (two real openings). Cases are taken from the Sheet
# backtest, tools/backtest_cross_source_dedup.py.

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state import company_key, content_key, location_key, title_key  # noqa: E402


@pytest.mark.parametrize("a, b", [
    ("Dragos", "Dragos, Inc."),
    ("Dragos", "DRAGOS INC"),
    ("The Squires Group, Inc.", "Squires Group"),
    ("The Mitre Corporation", "The MITRE Corporation"),
    ("Booz Allen Hamilton Inc.", "Booz Allen Hamilton"),
])
def test_company_key_ignores_legal_form_and_case(a, b):
    assert company_key(a) == company_key(b)


@pytest.mark.parametrize("a, b", [
    ("Zeno Group", "Zeno"),           # "Group" is part of the name, not a legal form
    ("Lumen Technologies", "Lumen"),
    ("Applied Intuition", "Applied Materials"),
])
def test_company_key_keeps_distinguishing_words(a, b):
    assert company_key(a) != company_key(b)


@pytest.mark.parametrize("a, b", [
    ("VP of IT", "Vice President, Information Technology"),
    ("Director, IT", "Director of Information Technology"),
    ("Director, Information Technology (IT)", "Director of Information Technology"),
    ("Sr. Director, IT Ops", "Senior Director of IT Operations"),
    ("IT Director-AI and Automation", "IT Director - AI and Automation"),
    ("Head of Security & IT", "Head of Security and IT"),
    ("Senior Director- Chief of Staff", "Senior Director Chief of Staff"),
    ("VP, IT (Remote)", "VP of IT"),
])
def test_title_key_collides_on_the_same_role(a, b):
    assert title_key(a) == title_key(b)


@pytest.mark.parametrize("a, b", [
    ("Director, Enterprise Operations", "Director, IT"),
    ("Director of IT", "Director of IT Operations"),
    ("VP, IT Business Solutions", "VP, IT"),
    # A requisition number in the title is the only thing telling two MetroStar
    # openings apart, so it has to survive normalisation.
    ("Sr. Vice President, Operations (6821)", "Sr. Vice President, Operations (6820)"),
])
def test_title_key_keeps_different_roles_apart(a, b):
    assert title_key(a) != title_key(b)


@pytest.mark.parametrize("a, b", [
    ("Hanover, MD", "Hanover, Maryland, US"),
    ("Hanover, MD", "Hanover, Anne Arundel County"),     # Adzuna's county form
    ("Washington, DC", "Washington, Washington, D.C."),
    ("Washington, DC", "Washington, District of Columbia"),
    ("New York, NY", "New York, New York"),
    ("New York, NY", "New York City"),
    ("Remote", "United States"),
    ("Remote - US", ""),
    ("Remote, USA", "Remote"),
    ("Austin, TX; Denver, CO", "Denver, CO; Austin, TX"),  # order must not matter
])
def test_location_key_collides_on_the_same_place(a, b):
    assert location_key(a) == location_key(b)


@pytest.mark.parametrize("a, b", [
    ("Baltimore, MD", "Rockville, MD"),
    ("Washington, DC", "Baltimore, MD"),
    ("Arlington, VA", "Reston, VA"),
    ("Boston, MA", "New York, NY"),
    ("Maryland", "Virginia"),
    ("Baltimore, MD", "Remote"),      # a placed location never matches a blank one
])
def test_location_key_keeps_different_places_apart(a, b):
    assert location_key(a) != location_key(b)


def test_location_key_metro_level_merges_one_metro():
    """The looser option, kept available but not the default."""
    assert (location_key("Arlington, VA", level="metro")
            == location_key("Washington, DC", level="metro"))
    assert (location_key("Baltimore, MD", level="metro")
            != location_key("Washington, DC", level="metro"))


def test_content_key_merges_the_dragos_postings():
    """The case FIL-10 exists for: one opening, three sources, three URLs."""
    keys = {content_key(job) for job in (
        {"company": "Dragos", "title": "Vice President, Information Technology",
         "location": "Hanover, Anne Arundel County"},
        {"company": "Dragos, Inc.", "title": "Vice President, Information Technology",
         "location": "Hanover, MD"},
        {"company": "Dragos", "title": "VP, IT", "location": "Hanover, MD"},
    )}
    assert len(keys) == 1


def test_content_key_needs_a_company_and_a_title():
    assert content_key({"company": "", "title": "Director, IT", "location": "Remote"}) is None
    assert content_key({"company": "Dragos", "title": "", "location": "Remote"}) is None
