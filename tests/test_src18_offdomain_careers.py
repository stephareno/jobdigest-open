"""SRC-18: which Bespoke careers URLs sit off the company's own website."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sourcing"))

from offdomain_careers import bespoke_eligible, registrable, why_kept  # noqa: E402


def test_registrable_domain_ignores_www_scheme_and_subdomains():
    assert registrable("https://www.umbra.space/careers") == "umbra.space"
    assert registrable("careers.rtx.com/global") == "rtx.com"
    assert registrable("http://umbraco.com/") != registrable("https://umbra.space")


def test_kept_urls_are_explained():
    assert why_kept("https://acme.com", "https://recruiting.paylocity.com/recruiting/jobs/All/x", None) \
        == "ATS / job-board host"
    assert why_kept("https://oldname.com", "https://newname.com/careers", "www.newname.com") \
        == "rebrand (website redirects there)"
    assert why_kept("http://www.raytheon.com", "https://careers.rtx.com/global/en/raytheon", "www.raytheon.com") \
        == "linked from the site (parent company or other)"


def test_only_readable_page_reasons_are_bespoke_eligible():
    assert bespoke_eligible({"careers_url": "https://acme.com/jobs", "reason": "bespoke_page"})
    assert bespoke_eligible({"careers_url": "https://acme.com/jobs", "reason": "unsupported_ats:icims"})
    assert not bespoke_eligible({"careers_url": "", "reason": "no_site"})
    assert not bespoke_eligible({"careers_url": "https://acme.com", "reason": "wrong_company_board"})


def test_one_word_bamboohr_slug_guess_is_not_accepted():
    """SRC-18: BambooHR serves a shared demo board for dead tenants, so a
    one-word name must not resolve there from a guess."""
    from tools.resolve_ats import SlugCandidate, slug_hit_accepted
    demo = {"result": [{"jobOpeningName": "IT Security Engineer", "departmentLabel": "IT"},
                       {"jobOpeningName": "Software Engineer", "departmentLabel": "Product"}]}
    assert not slug_hit_accepted("Worthy", "bamboohr", SlugCandidate("worthy", False), demo)
    # A multi-word name still resolves when the board names it.
    board = {"result": [{"jobOpeningName": "Staff Engineer", "departmentLabel": "Bask Health"}]}
    assert slug_hit_accepted("Bask Health", "bamboohr", SlugCandidate("baskhealth", True), board)
