"""PUB-22: the maintainer's About note.

The README's "## About" section and the page's "From the author" card carry
the note verbatim; the export scrub allows the maintainer's name only in
LICENSE and inside those two blocks (matched by block, not by file), and still
fails on the name anywhere else in either file or in any other shipped file.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "export"))

import export_public as ep  # noqa: E402

# Staged at docs/public/README.md in v2; shipped at the root of the export.
README_PATH = next(p for p in (ROOT / "docs" / "public" / "README.md", ROOT / "README.md") if p.exists())
# The note, with the maintainer's name left out so this file passes the scrub too.
NOTE = ("I'm {name}, an IT executive between roles. I built this for my own search and then "
        "made it public because the problem isn't unique. If it helps you find something good, I'd love "
        "to hear about it on {link}. And if you're hiring, feel free to say hello, too :)")


def _name() -> str:
    """The name as the README's About block gives it."""
    return re.search(r"^## About\n\nI'm ([^,]+),", README_PATH.read_text(), re.M).group(1)


def _url() -> str:
    return f"https://www.linkedin.com/in/{_name().lower().replace(' ', '-')}-665737b2/"


NAME = [("candidate name", "Pat Example", frozenset({"LICENSE"})),
        ("short name", "Pat", frozenset({"LICENSE"}))]

README = ("# Tool\n\nIntro.\n\n## About\n\nI'm Pat Example. Say hi, Pat fans.\n\n"
          "## License\n\nMIT.\n")
PAGE = ('<div class="wrap">\n<section>\n  <h2>Cost</h2>\n</section>\n\n'
        '<section id="from-the-author">\n  <div class="card"><h4>From the author</h4>'
        "<p>I'm Pat Example.</p></div>\n</section>\n\n<footer>MIT.</footer>\n")


def _tree(tmp_path, readme=README, page=PAGE, extra=None):
    (tmp_path / "LICENSE").write_text("Copyright Pat Example\n")
    (tmp_path / "README.md").write_text(readme)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.html").write_text(page)
    for rel, text in (extra or {}).items():
        (tmp_path / rel).write_text(text)
    return tmp_path


# --- The note ----------------------------------------------------------------------


def test_readme_about_sits_directly_above_license_verbatim():
    text = README_PATH.read_text()
    about = "## About\n\n" + NOTE.format(name=_name(), link=f"[LinkedIn]({_url()})") + "\n\n## License\n"
    assert about in text
    assert text.count("## About") == 1


def test_page_card_is_last_before_the_footer_with_a_safe_new_tab_link():
    text = (ROOT / "docs" / "index.html").read_text()
    link = f'<a href="{_url()}" target="_blank" rel="noopener">LinkedIn</a>'
    card = ('<section id="from-the-author">\n  <div class="card"><h4>From the author</h4><p>'
            + NOTE.format(name=_name(), link=link) + "</p></div>\n</section>\n\n<footer>")
    assert card in text
    assert text.count("From the author") == 1
    # Same card style as Cost and limits; no new colours.
    css = text.split("</style>")[0]
    assert "from-the-author" not in css


def test_the_name_appears_nowhere_else_in_the_staged_readme_or_page():
    words = [re.compile(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", re.I) for w in _name().split()]
    for path, shipped in ((README_PATH, "README.md"), (ROOT / "docs" / "index.html", "docs/index.html")):
        lines = path.read_text().splitlines()
        about = ep.author_block(shipped, lines)
        assert about, shipped
        rel = shipped
        outside = [n for n, line in enumerate(lines, 1) if n not in about and any(w.search(line) for w in words)]
        assert outside == [], (rel, outside)


# --- The scrub ----------------------------------------------------------------------


def test_scrub_allows_the_name_in_licence_and_the_two_about_blocks(tmp_path):
    assert ep.scrub(_tree(tmp_path), NAME) == []


def test_scrub_fails_on_the_name_elsewhere_in_the_same_two_files(tmp_path):
    readme = README.replace("Intro.", "Intro by Pat Example.") + "\nMaintained by Pat.\n"
    page = PAGE.replace("<h2>Cost</h2>", "<h2>Cost, by Pat</h2>").replace(
        "<footer>MIT.</footer>", "<footer>MIT. Pat Example</footer>")
    hits = ep.scrub(_tree(tmp_path, readme, page), NAME)
    assert sorted(hits) == sorted(["README.md:3: candidate name 'Pat Example'",
                                   "README.md:3: short name 'Pat'",
                                   "README.md:13: short name 'Pat'",
                                   "docs/index.html:3: short name 'Pat'",
                                   "docs/index.html:10: candidate name 'Pat Example'",
                                   "docs/index.html:10: short name 'Pat'"])


def test_scrub_fails_on_the_name_in_any_other_file_even_inside_an_about_heading(tmp_path):
    hits = ep.scrub(_tree(tmp_path, extra={"AGENTS.md": README}), NAME)
    assert "AGENTS.md:7: candidate name 'Pat Example'" in hits
    assert all(h.startswith("AGENTS.md:") for h in hits)


def test_an_unclosed_block_exempts_nothing(tmp_path):
    readme = "# Tool\n\n## About\n\nI'm Pat Example.\n"
    page = '<section id="from-the-author">\n<p>Pat Example</p>\n'
    hits = ep.scrub(_tree(tmp_path, readme, page), NAME)
    assert "README.md:5: candidate name 'Pat Example'" in hits
    assert "docs/index.html:2: candidate name 'Pat Example'" in hits


def test_the_exemption_covers_the_name_only_not_other_denylisted_values(tmp_path):
    readme = README.replace("I'm Pat Example.", "I'm Pat Example at Acme.")
    items = NAME + [("employer", "Acme", frozenset())]
    assert ep.scrub(_tree(tmp_path, readme), items) == ["README.md:7: employer 'Acme'"]
