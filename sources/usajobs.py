# ============================================================
# sources/usajobs.py — Federal postings from the USAJOBS search API (PRD §2.12)
# ============================================================
#
# Federal agencies post on usajobs.gov, not on an ATS we fetch (SRC-20: 3 of
# the 16 LinkedIn-first 7+ rows since 1 Sep were NASA and DISA). Runs with the
# once-a-day sources.
#
# Two passes per run, both limited to hiring paths open to the public and SES
# and to the last USAJOBS_DATE_POSTED_DAYS days (dedup absorbs the overlap):
#   1. the job series (profile sources.usajobs.series; 2210, IT Management)
#   2. one Keyword search per title in sources.usajobs.title_keywords.
#      Keyword is full-text, so a hit is kept only when the title itself names
#      one of those titles: the first live run (6 Oct) drew 118 VA physician
#      postings through "Chief Information Officer" in their body text, all
#      scored 0-1 at a Claude call each. A 2210 posting is kept by pass 1 anyway.
# Nothing is filtered by location at the API (CLAUDE.md rule 2: location is
# filtered post-fetch). USAJOBS is NOT title-gated (PRD §3): the gate drops
# proven matches such as DISA's "Supervisory IT Specialist" at GS-15. Cost is
# bounded instead by series, hiring path and the pay floor: a posting is kept
# only when the top of its annual pay range is at or above the pay floor
# (profile sources.usajobs.pay_floor_from; the comp floor by default).
#
# Company: OrganizationName (the hiring agency as the posting names it —
# "Defense Information Systems Agency", not "Department of Defense"), except
# when it names an office or headquarters inside a department rather than an
# agency ("Headquarters, NASA", "Offices, Boards and Divisions", "Deputy
# Assistant Secretary for Information and Technology"): then DepartmentName.
#
# Location: every PositionLocation, joined with "; " so location.py resolves
# each and the most favourable tier counts (FIL-09). Remote when the posting's
# RemoteIndicator is true or a location reads "Anywhere in the U.S. (remote
# job)"; TeleworkEligible alone is Hybrid, never remote; otherwise In-office.
#
# Apply link: the announcement page, https://www.usajobs.gov/job/<control no.>.
#
# Needs USAJOBS_API_KEY and GMAIL_ADDRESS (the User-Agent must be the email the
# key is registered to). Either missing raises — run_source logs it and the
# fetch continues without USAJOBS (CLAUDE.md rule 9).
#
# Standalone: `python3 -m sources.usajobs` prints the run's counts;
# `--save-fixture PATH` writes the raw response of the first series page, for
# refreshing tests/fixtures/usajobs_search.json from the live API.

import json
import os
import re
import sys
import time

import requests

import profile_loader
from config import USAJOBS_DATE_POSTED_DAYS, USAJOBS_MAX_PAGES, USAJOBS_RESULTS_PER_PAGE
from sheet_dates import normalize_posted

PAY_FLOOR = profile_loader.usajobs_pay_floor()
# PUB-05: what to search, from the profile's sources.usajobs.
_SEARCH = profile_loader.active()["sources"]["usajobs"]
USAJOBS_JOB_SERIES = _SEARCH["series"]
USAJOBS_HIRING_PATHS = ";".join(_SEARCH["hiring_paths"])      # the API's "public;ses" form
USAJOBS_TITLE_KEYWORDS = tuple(_SEARCH["title_keywords"])

SEARCH_URL = "https://data.usajobs.gov/api/search"
HOST = "data.usajobs.gov"
JOB_URL = "https://www.usajobs.gov/job/{}"
ANNUAL_RATE_CODES = ("PA", "PER YEAR")  # RateIntervalCode for an annual salary
REMOTE_LOCATION = "anywhere in the u.s. (remote job)"
# OrganizationName values that are an office inside a department, not an agency.
_OFFICE_NAME_RE = re.compile(
    r"\bHeadquarters\b|^(Immediate\s+)?Offices?\b(?!\s+of\s+Personnel\s+Management)"
    r"|^(Deputy\s+|Principal\s+Deputy\s+)?(Under|Assistant)\s+Secretary\b", re.I)
_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in USAJOBS_TITLE_KEYWORDS) + r")\b", re.I)
REQUEST_TIMEOUT = 30
PAGE_DELAY_SECS = 0.5


def _headers():
    key = os.environ.get("USAJOBS_API_KEY", "").strip()
    email = os.environ.get("GMAIL_ADDRESS", "").strip()
    missing = [n for n, v in (("USAJOBS_API_KEY", key), ("GMAIL_ADDRESS", email)) if not v]
    if missing:
        raise RuntimeError(f"[USAJOBS] {', '.join(missing)} not set — USAJOBS cannot be fetched "
                           f"(the key comes from developer.usajobs.gov; the User-Agent must be "
                           f"the email it is registered to)")
    return {"Host": HOST, "User-Agent": email, "Authorization-Key": key}


def search_passes():
    """(label, params) for each search a run makes."""
    base = {"HiringPath": USAJOBS_HIRING_PATHS, "DatePosted": USAJOBS_DATE_POSTED_DAYS,
            "ResultsPerPage": USAJOBS_RESULTS_PER_PAGE}
    passes = [(f"series {USAJOBS_JOB_SERIES}", dict(base, JobCategoryCode=USAJOBS_JOB_SERIES))]
    passes += [(f"keyword {kw!r}", dict(base, Keyword=kw)) for kw in USAJOBS_TITLE_KEYWORDS]
    return passes


def _get_page(session, headers, params, page):
    resp = session.get(SEARCH_URL, headers=headers, params=dict(params, Page=page),
                       timeout=REQUEST_TIMEOUT)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"[USAJOBS] HTTP {resp.status_code}: key rejected — check "
                           f"USAJOBS_API_KEY and that GMAIL_ADDRESS is the email it is registered to")
    resp.raise_for_status()
    return resp.json()


def _items(data):
    return ((data or {}).get("SearchResult") or {}).get("SearchResultItems") or []


def _pages(data):
    user_area = ((data or {}).get("SearchResult") or {}).get("UserArea") or {}
    try:
        return int(user_area.get("NumberOfPages") or 1)
    except (TypeError, ValueError):
        return 1


def fetch_pass(session, headers, label, params):
    """Every item a search returns, across pages. A failed page ends the pass
    with what it has and logs it; the other passes still run."""
    items, page = [], 1
    while page <= USAJOBS_MAX_PAGES:
        try:
            data = _get_page(session, headers, params, page)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001 — logged, the pass keeps what it has
            print(f"[USAJOBS] {label}: page {page} failed ({e}) — keeping {len(items)} from "
                  f"earlier pages")
            break
        batch = _items(data)
        items.extend(batch)
        if not batch or page >= _pages(data):
            break
        page += 1
        time.sleep(PAGE_DELAY_SECS)
    else:
        print(f"[USAJOBS] {label}: stopped at USAJOBS_MAX_PAGES ({USAJOBS_MAX_PAGES})")
    print(f"[USAJOBS] {label}: {len(items)} postings")
    return items


def title_names_keyword(item):
    """True when a keyword-pass hit carries one of the searched titles in its own title."""
    title = (item.get("MatchedObjectDescriptor") or {}).get("PositionTitle") or ""
    return bool(_TITLE_RE.search(title))


def agency(descriptor):
    org = str(descriptor.get("OrganizationName") or "").strip()
    dept = str(descriptor.get("DepartmentName") or "").strip()
    if not org or (dept and _OFFICE_NAME_RE.search(org)):
        return dept or org
    return org


def annual_pay_top(descriptor):
    """Top of the posted annual pay range, or None when no annual range is posted."""
    tops = []
    for rem in descriptor.get("PositionRemuneration") or []:
        if str(rem.get("RateIntervalCode", "")).strip().upper() not in ANNUAL_RATE_CODES:
            continue
        try:
            tops.append(float(str(rem.get("MaximumRange", "")).replace(",", "")))
        except ValueError:
            continue
    return max(tops) if tops else None


def _comp_text(descriptor):
    for rem in descriptor.get("PositionRemuneration") or []:
        if str(rem.get("RateIntervalCode", "")).strip().upper() in ANNUAL_RATE_CODES:
            try:
                low = float(str(rem.get("MinimumRange", "")).replace(",", ""))
                high = float(str(rem.get("MaximumRange", "")).replace(",", ""))
            except ValueError:
                continue
            return f"${int(low):,} – ${int(high):,}"
    return ""


def _details(descriptor):
    return ((descriptor.get("UserArea") or {}).get("Details")) or {}


def _flag(value):
    return value is True or str(value).strip().lower() in ("true", "yes", "y")


def work_model(descriptor):
    details = _details(descriptor)
    locations = [str(l.get("LocationName", "")).strip().lower()
                 for l in descriptor.get("PositionLocation") or []]
    if _flag(details.get("RemoteIndicator")) or _flag(descriptor.get("RemoteIndicator")) \
            or REMOTE_LOCATION in locations:
        return "Remote"
    if _flag(details.get("TeleworkEligible")):
        return "Hybrid"
    return "In-office"


def _description(descriptor):
    details = _details(descriptor)
    duties = details.get("MajorDuties") or []
    if isinstance(duties, str):
        duties = [duties]
    parts = [str(details.get("JobSummary") or "").strip()] + [str(d).strip() for d in duties]
    return "\n\n".join(p for p in parts if p)


def to_job(item):
    """One USAJOBS search item as a pipeline job dict."""
    d = item.get("MatchedObjectDescriptor") or {}
    control = str(item.get("MatchedObjectId") or d.get("PositionID") or "").strip()
    posted, precision = normalize_posted(d.get("PublicationStartDate"))
    close, _ = normalize_posted(d.get("ApplicationCloseDate"))
    return {
        "title": str(d.get("PositionTitle") or "").strip(),
        "company": agency(d),
        "location": "; ".join(str(l.get("LocationName", "")).strip()
                              for l in d.get("PositionLocation") or []
                              if str(l.get("LocationName", "")).strip()),
        "work_model": work_model(d),
        "comp": _comp_text(d),
        "apply_link": JOB_URL.format(control) if control else str(d.get("PositionURI") or ""),
        "description": _description(d),
        "date_posted": posted,
        "posted_precision": precision,
        "close_date": close,  # kept for a later digest change; not rendered yet
        "source": "usajobs",
    }


def _grade(descriptor):
    """Pay plan and top grade, e.g. "GS-15", "ES" for SES; "?" when not posted."""
    plans = [str(g.get("Code") or "").strip() for g in descriptor.get("JobGrade") or []]
    high = str(_details(descriptor).get("HighGrade") or "").strip()
    plan = plans[0] if plans and plans[0] else "?"
    return f"{plan}-{high}" if high and high not in ("0", "00") else plan


def select(items, min_comp=None):
    """(jobs kept, counts). One job per control number; kept only when the top of
    its annual range is at or above the comp floor."""
    floor = PAY_FLOOR if min_comp is None else min_comp
    counts = {"fetched": 0, "below_floor": 0, "no_annual_pay": 0, "kept": 0,
              "below_floor_by_grade": {}}
    jobs, seen = [], set()
    for item in items:
        control = str(item.get("MatchedObjectId") or "").strip()
        if control and control in seen:
            continue
        seen.add(control)
        counts["fetched"] += 1
        top = annual_pay_top(item.get("MatchedObjectDescriptor") or {})
        if top is None:
            counts["no_annual_pay"] += 1
            continue
        if top < floor:
            counts["below_floor"] += 1
            grade = _grade(item.get("MatchedObjectDescriptor") or {})
            counts["below_floor_by_grade"][grade] = counts["below_floor_by_grade"].get(grade, 0) + 1
            continue
        jobs.append(to_job(item))
        counts["kept"] += 1
    return jobs, counts


def fetch_all():
    headers = _headers()
    items = []
    off_title = 0
    with requests.Session() as session:
        for label, params in search_passes():
            got = fetch_pass(session, headers, label, params)
            if "Keyword" in params:
                kept = [i for i in got if title_names_keyword(i)]
                off_title += len(got) - len(kept)
                got = kept
            items.extend(got)
    jobs, c = select(items)
    grades = ", ".join(f"{g}: {n}" for g, n in sorted(c["below_floor_by_grade"].items()))
    print(f"[USAJOBS] keyword hits without the title in the job title, dropped: {off_title}")
    print(f"[USAJOBS] fetched={c['fetched']} (unique announcements)  "
          f"dropped below pay floor ${PAY_FLOOR:,}={c['below_floor']}  "
          f"no annual pay={c['no_annual_pay']}  passed to scoring={c['kept']}")
    if grades:
        print(f"[USAJOBS] below pay floor by grade: {grades}")
    return jobs


def _cli():
    if "--save-fixture" in sys.argv:
        path = sys.argv[sys.argv.index("--save-fixture") + 1]
        label, params = search_passes()[0]
        with requests.Session() as session:
            data = _get_page(session, _headers(), params, 1)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
        print(f"[USAJOBS] wrote {len(_items(data))} items from {label}, page 1, to {path}")
        return
    for job in fetch_all():
        print(f"  {job['company']} | {job['title']} | {job['comp']} | {job['work_model']} | "
              f"{job['location'][:80]} | {job['apply_link']}")


if __name__ == "__main__":
    _cli()
