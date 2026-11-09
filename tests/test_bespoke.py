"""Tests for sources/bespoke.py — change detection and the content check.

Two things here are load-bearing and easy to break silently:

  * the fingerprint. Too loose and a page never looks changed, so a new opening
    is never seen; too tight and every check bills a model call for nothing.
  * the content check. A careers page listing other organisations' openings is
    common (subsidiary directories, portfolio pages), and without this test a
    regression there would file another company's jobs under the site's owner.

No network access — every case is a literal page.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources.bespoke as bespoke  # noqa: E402
from sources.bespoke import (  # noqa: E402
    company_matches,
    job_region_fingerprint,
    listing_is_theirs,
    job_anchors,
    to_job_dicts,
    visible_text,
    word_count,
)
from tools.resolve_ats import _normalize_text  # noqa: E402

URL = "https://acme.example.com/careers"


def page(body: str) -> str:
    return f"<html><head><title>Careers</title></head><body>{body}</body></html>"


LISTINGS = """
  <a href="/careers/director-of-it">Director of IT</a>
  <a href="/careers/staff-engineer">Staff Engineer</a>
"""


# ----------------------------------------------------------- fingerprinting

def test_fingerprint_is_stable_across_identical_pages():
    assert job_region_fingerprint(page(LISTINGS), URL) == \
           job_region_fingerprint(page(LISTINGS), URL)


def test_volatile_values_do_not_read_as_a_change():
    """A CSRF token and a render timestamp change on every request. If they
    counted, every page would bill an extraction on every single check."""
    a = page(LISTINGS + '<input name="csrf_token" value="a1b2c3d4e5f6a7b8c9d0">'
                        '<p>Generated 2026-08-27T09:15:00Z</p>')
    b = page(LISTINGS + '<input name="csrf_token" value="99887766554433221100">'
                        '<p>Generated 2026-08-27T11:42:13Z</p>')
    assert job_region_fingerprint(a, URL) == job_region_fingerprint(b, URL)


def test_a_new_opening_does_read_as_a_change():
    more = LISTINGS + '<a href="/careers/vp-infrastructure">VP Infrastructure</a>'
    assert job_region_fingerprint(page(LISTINGS), URL) != \
           job_region_fingerprint(page(more), URL)


def test_a_closed_opening_reads_as_a_change():
    fewer = '<a href="/careers/director-of-it">Director of IT</a>'
    assert job_region_fingerprint(page(LISTINGS), URL) != \
           job_region_fingerprint(page(fewer), URL)


def test_chrome_changes_do_not_read_as_a_change():
    """Nav, footer and a cookie banner are page furniture, not listings."""
    a = page("<nav>Home About</nav>" + LISTINGS +
             '<div class="cookie-consent">Accept cookies</div><footer>© 2025</footer>')
    b = page("<nav>Home About Blog Press</nav>" + LISTINGS +
             '<div class="cookie-consent">We value your privacy</div><footer>© 2026</footer>')
    assert job_region_fingerprint(a, URL) == job_region_fingerprint(b, URL)


def test_pages_without_job_links_still_diff_on_their_text():
    """Roles listed as headings have no links to compare. Hashing nothing would
    mean never noticing such a company hire."""
    a = page("<h2>Open roles</h2><p>Director of IT</p>")
    b = page("<h2>Open roles</h2><p>Director of IT</p><p>VP Infrastructure</p>")
    assert job_region_fingerprint(a, URL) != job_region_fingerprint(b, URL)


# ------------------------------------------------------------- page reading

def test_visible_text_drops_scripts_and_styles():
    text = visible_text(page("<script>var x=1</script><style>.a{}</style><p>Hello</p>"))
    assert "Hello" in text and "var x" not in text and ".a{" not in text


def test_word_count_sees_a_javascript_shell_as_near_empty():
    shell = page('<div id="root"></div><script>renderApp()</script>')
    assert word_count(visible_text(shell)) < 150


def test_job_anchors_resolve_relative_hrefs():
    urls = dict((label, u) for u, label in job_anchors(page(LISTINGS), URL))
    assert urls["Director of IT"] == "https://acme.example.com/careers/director-of-it"


# ------------------------------------------------------------ content check

def test_company_matches_accepts_the_obvious_forms():
    assert company_matches("Acme", "Acme Robotics")
    assert company_matches("Acme Robotics Inc", "Acme Robotics")
    assert company_matches("CGC", "Coalition for Green Capital")


def test_company_matches_rejects_a_different_employer():
    assert not company_matches("Air Force One", "Service Logic")


def _check(job, company, html=None):
    html = html or page(LISTINGS)
    return listing_is_theirs(job, company,
                             _normalize_text(visible_text(html)),
                             {u for u, _ in job_anchors(html, URL)})


def test_a_real_listing_passes():
    assert _check({"title": "Director of IT", "company": "Acme"}, "Acme") == ""


def test_an_invented_title_is_caught():
    assert _check({"title": "Chief Robot Officer", "company": "Acme"}, "Acme") \
        == "title_not_on_page"


def test_another_employers_job_is_caught():
    """The failure that matters: a page listing 40+ other firms' openings must
    never contribute them to the company whose site they sit on."""
    assert _check({"title": "Director of IT", "company": "Globex"}, "Acme") \
        == "other_employer"


def test_a_fabricated_url_is_caught():
    job = {"title": "Director of IT", "company": "Acme",
           "url": "https://acme.example.com/jobs/director-of-it"}
    assert _check(job, "Acme") == "url_not_on_page"


# ------------------------------------------------- hand-off to the funnel

def test_to_job_dicts_applies_the_title_gate_and_shapes_the_job():
    counters = {k: 0 for k in (
        "content_check_ok", "passed_title_filter", "dropped_title_filter",
        "rejected_no_title", "rejected_title_not_on_page", "rejected_no_company",
        "rejected_other_employer", "rejected_url_not_on_page")}
    raw = [
        {"title": "Director of IT", "location": "Remote", "url": "", "company": "Acme"},
        {"title": "Staff Engineer", "location": "", "url": "", "company": "Acme"},
    ]
    jobs = to_job_dicts(raw, "Acme", page(LISTINGS), URL, counters)
    assert [j["title"] for j in jobs] == ["Director of IT"]
    assert counters["content_check_ok"] == 2
    assert counters["dropped_title_filter"] == 1
    job = jobs[0]
    assert job["source"] == "bespoke" and job["tier"] == "bespoke"
    assert job["is_dream_company"] is False
    assert job["work_model"] == "remote"
    assert job["apply_link"] == URL   # no per-job link on the page: fall back


def test_category_headings_are_left_for_the_title_gate():
    """A known artefact: "ROLES WE HIRE" headings extract as listings. They are
    verbatim on the page and correctly attributed, so the content check passes
    them — the title gate is what drops them, and that is deliberate."""
    counters = {k: 0 for k in (
        "content_check_ok", "passed_title_filter", "dropped_title_filter",
        "rejected_no_title", "rejected_title_not_on_page", "rejected_no_company",
        "rejected_other_employer", "rejected_url_not_on_page")}
    html = page("<h3>Software &amp; ML</h3><h3>Simulation &amp; 3D</h3>")
    raw = [{"title": "Software & ML", "company": "Acme"},
           {"title": "Simulation & 3D", "company": "Acme"}]
    assert to_job_dicts(raw, "Acme", html, URL, counters) == []
    assert counters["dropped_title_filter"] == 2


# ------------------------------------------------------------ rolling pass

import threading  # noqa: E402

READABLE = page("<p>" + "words " * 200 + "</p>" + LISTINGS)


def _companies(n):
    return [{"company": f"Co{i}", "url": f"https://co{i}.example.com/careers", "reason": "bespoke_page"}
            for i in range(n)]


def _checker(status=200, html=READABLE, calls=None):
    """A check function that answers every company without the network."""
    def check(batch):
        out = []
        for c in batch:
            if calls is not None:
                calls.append(c["company"])
            r = bespoke.CheckResult(c["company"], c["url"])
            r.status, r.html, r.final_url = status, html, c["url"]
            r.fingerprint = job_region_fingerprint(html, c["url"])
            r.words = word_count(visible_text(html))
            r.changed, r.note = True, "changed"
            out.append(r)
        return out
    return check


class _Clock:
    """Fake monotonic clock: each extraction costs `per_extract` seconds."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _extractor(clock, per_extract=10.0, error=""):
    def extract(company, html, url, model=None):
        clock.t += per_extract
        if error:
            return [], 0, 0, error
        return [{"title": "Director of IT", "location": "Remote, US",
                 "url": "", "company": company}], 1, 1, ""
    return extract


def test_rolling_pass_stops_at_budget_and_resumes_at_the_next_company(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=10.0))
    monkeypatch.setattr(bespoke, "company_matches", lambda claimed, name: True)
    monkeypatch.setattr(bespoke, "listing_is_theirs", lambda *a, **k: "")
    companies = _companies(40)
    ordered = sorted(c["company"] for c in companies)
    ordered = [c["company"] for c in sorted(companies, key=lambda c: bespoke.order_key(c["company"]))]

    first = bespoke.run_rolling(companies, {}, {}, budget_secs=100, api_ready=True,
                                clock=clock, check=_checker())
    assert first.stop_reason == "budget"
    done = list(first.entries)
    assert done == [c.lower() for c in ordered[:len(done)]]  # a contiguous prefix, in order
    assert all(e.get("last_extracted") for e in first.entries.values())
    assert len(first.jobs) == len(done)  # every done company's postings are handed over

    clock.t = 0.0
    state = dict(first.entries)
    second = bespoke.run_rolling(companies, state, first.cursor, budget_secs=100, api_ready=True,
                                 clock=clock, check=_checker())
    assert list(second.entries)[0] == ordered[len(done)].lower()  # resumes exactly after the cursor


def test_a_full_pass_wraps_and_counts(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=0.0))
    monkeypatch.setattr(bespoke, "listing_is_theirs", lambda *a, **k: "")
    companies = _companies(10)
    run = bespoke.run_rolling(companies, {}, {}, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker())
    assert run.stop_reason == ""
    assert run.cursor["passes_completed"] == 1
    assert run.cursor["pass"] == 2 and run.cursor["position"] == ""
    assert len(run.entries) == 10


def test_unchanged_pages_cost_no_extraction(monkeypatch):
    clock = _Clock()
    calls = []

    def never(*a, **k):
        calls.append(a)
        return [], 0, 0, ""

    monkeypatch.setattr(bespoke, "extract_jobs", never)
    companies = _companies(5)
    state = {c["company"].lower(): {"last_extracted": "2026-09-16",
                                    "fingerprint": job_region_fingerprint(READABLE, c["url"])}
             for c in companies}

    def unchanged(batch):
        results = _checker()(batch)
        for r in results:
            r.changed = False
        return results

    run = bespoke.run_rolling(companies, state, {}, budget_secs=1000, api_ready=True,
                              clock=clock, check=unchanged)
    assert calls == []
    assert len(run.entries) == 5


def test_api_outage_stops_the_pass_without_skipping_companies(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=1.0, error="api_error: 529"))
    companies = _companies(20)
    run = bespoke.run_rolling(companies, {}, {}, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker())
    assert run.stop_reason == "api_errors"
    # The failures before the streak limit are done (retried next pass as never
    # extracted); the company that hit the limit is not, so the pass resumes there.
    assert len(run.entries) == bespoke.EXTRACTION_ERROR_STREAK - 1
    assert not any(e.get("last_extracted") for e in run.entries.values())


def test_cancelled_run_saves_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(bespoke, "STATE_PATH", tmp_path / "bespoke_state.json")
    monkeypatch.setattr(bespoke, "CURSOR_PATH", tmp_path / "bespoke_cursor.json")
    monkeypatch.setattr(bespoke, "load_companies", lambda: _companies(3))
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=0.0))
    monkeypatch.setattr(bespoke, "_api_ready", lambda: True)
    monkeypatch.setattr(bespoke, "check_all", lambda batch, state: _async(_checker()(batch)))
    jobs = bespoke.fetch_all()
    bespoke.fetch_all.cancel()           # main.run_source abandoned it
    jobs.commit()
    assert not (tmp_path / "bespoke_state.json").exists()
    assert not (tmp_path / "bespoke_cursor.json").exists()

    jobs = bespoke.fetch_all()           # a normal run commits after queueing
    jobs.commit()
    assert len(bespoke.load_state()) == 3
    assert bespoke.load_cursor()["passes_completed"] == 1


async def _async_value(value):
    return value


def _async(value):
    return _async_value(value)


def test_order_is_stable_across_csv_order():
    names = [f"Company {i}" for i in range(50)]
    assert sorted(names, key=bespoke.order_key) == sorted(reversed(names), key=bespoke.order_key)


def test_extractions_run_four_at_a_time_and_results_stay_in_order(monkeypatch):
    import time as _time
    live, peak, lock = [0], [0], threading.Lock()

    def slow_extract(company, html, url, model=None):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        _time.sleep(0.02)
        with lock:
            live[0] -= 1
        return [{"title": "Director of IT", "location": "", "url": "", "company": company}], 1, 1, ""

    monkeypatch.setattr(bespoke, "extract_jobs", slow_extract)
    monkeypatch.setattr(bespoke, "listing_is_theirs", lambda *a, **k: "")
    companies = _companies(30)
    run = bespoke.run_rolling(companies, {}, {}, budget_secs=1000, api_ready=True, check=_checker())
    assert peak[0] == 4
    ordered = [c["company"].lower() for c in sorted(companies, key=lambda c: bespoke.order_key(c["company"]))]
    assert list(run.entries) == ordered
    assert [j["company"].lower() for j in run.jobs] == ordered


def test_no_per_run_extraction_cap(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=0.0))
    monkeypatch.setattr(bespoke, "listing_is_theirs", lambda *a, **k: "")
    run = bespoke.run_rolling(_companies(400), {}, {}, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker())
    assert run.stats["extracted"] == 400                  # run_rolling with no cap given: uncapped
    # REV-06: fetch_all passes a cap (config default, profile override, daily ceiling).
    assert bespoke.config.BESPOKE_MAX_EXTRACTIONS_PER_RUN == 60
