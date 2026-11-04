"""PUB-18: the public page's structure and the samples' display sources.

Covers: a sample's "Source:" line sets the job's source key and an example.com
apply link, and a posting without one keeps calibrate's own source; every
sample names the source it stands in for; the saved sample results and both
rendered samples never say "calibrate"; docs/index.html keeps its document
skeleton, its four sections in order and six links that open in a new tab.
"""

import json
import re
from pathlib import Path

import calibrate

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "examples" / "postings"

SOURCES = {"01": "ats", "02": "ats", "03": "watchlist", "04": "ats", "05": "linkedin_email",
           "06": "bespoke", "07": "jsearch", "08": "adzuna", "09": "adzuna", "10": "jsearch",
           "11": "adzuna", "12": "adzuna"}


def test_a_source_line_sets_the_source_and_a_plausible_link(tmp_path):
    with_source = tmp_path / "a-sample.md"
    with_source.write_text("# Director of FP&A\nCompany: Acme\nSource: LinkedIn\n\nBody.\n")
    job = calibrate.parse_posting(str(with_source))
    assert job["source"] == "linkedin_email"           # a display name reads back as its key
    assert job["apply_link"] == "https://example.com/jobs/a-sample"
    assert "display_source" not in job and job["description"] == "Body."
    without = tmp_path / "b.md"
    without.write_text("# Director of FP&A\nCompany: Acme\n\nBody.\n")
    job = calibrate.parse_posting(str(without))
    assert job["source"] == "calibrate" and job["apply_link"] == "calibrate://b.md"


def test_every_sample_names_its_source():
    got = {p.name[:2]: calibrate.parse_posting(str(p))["source"] for p in sorted(SAMPLES.glob("*.md"))}
    assert got == SOURCES


def test_the_saved_results_and_renders_never_say_calibrate():
    data = json.loads((ROOT / "docs" / "assets" / "sample_calibrate.json").read_text())
    assert {j["file"][:2]: j["source"] for j in data["jobs"]} == SOURCES
    assert all(j["apply_link"].startswith("https://example.com/jobs/") for j in data["jobs"])
    for name in ("sample_digest.html", "sample_sheet.html"):
        assert "calibrate" not in (ROOT / "docs" / "assets" / name).read_text().lower(), name


def test_the_page_keeps_its_skeleton_sections_and_new_tab_links():
    page = (ROOT / "docs" / "index.html").read_text()
    assert page.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    sections = [re.sub(r"\s+", " ", h).strip() for h in re.findall(r"<h2[^>]*>([^<]*)", page)]
    assert [s.split(" each")[0] for s in sections] == ["What arrives", "How to Set Up", "Cost and limits",
                                                       "How it works"]
    links = re.findall(r'<a [^>]*target="_blank"[^>]*>', page)
    assert len(links) == 6 and all('rel="noopener"' in a for a in links)
