"""Regression tests for DEL-09: the "Cool company, wrong role" section shows
one card per company, then applies the cap. Main sections are untouched."""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GMAIL_ADDRESS", "test@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import emailer


def _job(company, title, score, dream=False):
    return {"company": company, "title": title, "score": score,
            "is_dream_company": dream, "apply_link": f"https://x/{title}",
            "source": "ATS"}


def _cool_section(html):
    return html[html.index("Cool company, wrong role"):]


def _cool_cards(html):
    return re.findall(r'<div style="font-size:14px;font-weight:600;color:#1a1a1a;">([^<]*)',
                      _cool_section(html))


def test_five_rows_two_companies_gives_two_cards():
    cool = [
        _job("Figma", "VP Sales", 4),
        _job("figma ", "Director, Marketing", 6),
        _job("Figma", "Head of Legal", 5),
        _job("Dragos", "VP Finance", 3),
        _job("Dragos", "Director, Partnerships", 5, dream=True),
    ]
    html = emailer._build_html([], [], cool, [], False, False)

    assert sorted(_cool_cards(html)) == ["Dragos", "figma "]
    section = _cool_section(html)
    # Best row per company: dream first, then score.
    assert "Director, Partnerships" in section and "VP Finance" not in section
    assert "Director, Marketing" in section
    assert "VP Sales" not in section and "Head of Legal" not in section
    assert "Cool company, wrong role (2)" in html
    assert "3 more logged to the Sheet only." in html


def test_cap_still_applies_across_distinct_companies():
    cool = [_job(f"Co{i}", f"Role{i}", i) for i in range(5)]
    html = emailer._build_html([], [], cool, [], False, False)

    assert len(_cool_cards(html)) == emailer._SCORING["cool_company_wrong_role_cap"] == 3
    assert "2 more logged to the Sheet only." in html


def test_main_section_not_deduped_and_cool_card_not_suppressed():
    main = [_job("Figma", f"VP IT {i}", 8) for i in range(3)]
    cool = [_job("Figma", "VP Sales", 4), _job("Figma", "Director, Marketing", 6)]
    html = emailer._build_html(main, [], cool, [], False, False)

    main_section = html[html.index("Top matches"):html.index("Cool company, wrong role")]
    for i in range(3):
        assert f"VP IT {i}" in main_section
    assert "Top matches (3)" in html
    assert _cool_cards(html) == ["Figma"]
    assert "1 more logged to the Sheet only." in html
