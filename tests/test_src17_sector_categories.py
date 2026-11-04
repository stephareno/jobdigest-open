"""SRC-17: Robotics, Space/Aerospace and Materials categories (sourcing/categories.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sourcing"))

from categories import MATERIALS, ROBOTICS, SPACE, recategorize, sector_of  # noqa: E402


@pytest.mark.parametrize("industries, description, company, expected", [
    # Industry values decide on their own.
    ("Robotics; Software", "", "Acme", ROBOTICS),
    ("Aerospace; Manufacturing", "", "Acme", SPACE),
    ("Advanced Materials", "", "Acme", MATERIALS),
    # Description phrases.
    ("Manufacturing", "Builds humanoid robots for warehouses.", "Acme", ROBOTICS),
    ("Telecommunications", "Operates a constellation of satellites in low Earth orbit.", "Acme", SPACE),
    ("Manufacturing", "A materials science company making low-carbon cement.", "Acme", MATERIALS),
    ("Manufacturing", "Develops cellulose-based materials for packaging.", "InventWood", MATERIALS),
    ("Recycling", "Li-ion recycling.", "Blue Whale Materials", MATERIALS),
    # Two sectors: the stronger signal wins (NASA lists Robotics among its industries).
    ("Aerospace; Government; Robotics; Space Travel", "The U.S. space agency.", "NASA", SPACE),
])
def test_sector_matches(industries, description, company, expected):
    assert sector_of(industries, description, company) == expected


@pytest.mark.parametrize("industries, description, company", [
    # "material" in unrelated senses
    ("Logistics; Software", "SaaS for materials handling in distribution centers.", "Acme"),
    ("Education; E-Learning", "Publishes course materials for community colleges.", "Acme"),
    ("Marketing", "Designs marketing materials for brokers.", "Acme"),
    ("Retail", "Distributes building materials and roofing products.", "Beacon"),
    ("Consulting", "Emergency response for hazardous materials incidents.", "Chemtrec"),
    # "robotic" meaning software bots
    ("Software", "Robotic process automation for back offices.", "Acme"),
    ("Consulting", "Advisory, robotic automation and analytics services.", "weElevateIT"),
    # "space" meaning a place or a market
    ("Real Estate", "Flexible office space and coworking for startups.", "Acme"),
    ("Software", "The leading platform in the HR tech space.", "Acme"),
    ("Health Care", "Primary care with satellite clinics across Ohio.", "Acme"),
    # The company name carries the sector word, the business does not.
    ("Legal; Legal Tech", "Rocket Lawyer provides online legal services.", "Rocket Lawyer"),
    ("Health Care", "Alloy is a telehealth company for women over 40.", "Alloy"),
    ("Software", "SpaceCraft provides web-based software for small businesses.", "SpaceCraft"),
    # Crunchbase's "defense & space company" boilerplate on a staffing contractor
    ("Information Technology", "TKC Global is a defense & space company specializing in IT and staffing.",
     "TKC Global"),
])
def test_false_positives_excluded(industries, description, company):
    assert sector_of(industries, description, company) is None


def test_recategorize_keeps_other_categories():
    assert recategorize("Fintech", "Payments", "Card issuing API.", "Acme") == "Fintech"
    assert recategorize("Hardware", "Robotics", "", "Acme") == ROBOTICS
    assert recategorize("GovTech/Defense", "Space Travel", "", "Acme") == SPACE


@pytest.mark.parametrize("company, industries, description, expected", [
    ("Nintex", "Robotic Process Automation (RPA); Robotics; Software",
     "Nintex enables enterprise IT professionals to manage business processes.", "B2B/SaaS"),
    ("MCA Connect", "Consumer Electronics; Information Technology; Robotics",
     "MCA Connect is a system integrator for Microsoft Dynamics.", "B2B/SaaS"),
    ("Egen Solutions", "Consulting; Robotics; Software", "Software consulting.", "B2B/SaaS"),
    ("Ashling Partners", "Artificial Intelligence (AI); Industrial Automation; Robotics",
     "Intelligent process automation and RPA.", "B2B/SaaS"),
    ("Digital Divide Data", "Artificial Intelligence (AI); Autonomous Vehicles",
     "Data labeling services for autonomous driving.", "AI/ML"),
])
def test_it_services_firms_tagged_robotics_are_relabelled(company, industries, description, expected):
    assert recategorize("Robotics", industries, description, company) == expected


def test_services_description_overrides_a_sector_tag_without_a_sector_phrase():
    # Tagged Robotics, describes consulting, names nothing robotic: not Robotics,
    # and not Hardware either (the harvest rule would send the Robotics tag there).
    assert sector_of("Robotics; Information Technology", "An IT consulting firm.", "Acme") is None
    assert recategorize("Robotics", "Robotics; Information Technology", "An IT consulting firm.",
                        "Acme") == "B2B/SaaS"
    # A services firm that does name robots stays Robotics.
    assert sector_of("Robotics", "Consulting on warehouse robots.", "Acme") == ROBOTICS
