# ============================================================
# sources/watchlist.py — Fallback for unresolved dream companies
# ============================================================
#
# Companies with a resolved ATS board are fetched by sources/ats.py; this
# source covers only the unresolved: bucket of data/companies.yaml, two ways:
#   1. Known Workday tenants (pinned.workday in data/companies.yaml) —
#      fetched via the Workday CXS JSON endpoint, preferred over scraping.
#   2. Everything else with a career URL (pinned.careers_pages in
#      data/companies.yaml) — HTML scrape of the career page.
# Both are hand-maintained (PUB-04, moved from config.py): keys are names
# exactly as in the dream-company list; tools/resolve_ats.py never touches
# pinned:. Careers URLs for currently resolved companies are dormant backups
# in case an ATS migration drops one back to unresolved. Workday tenants were
# verified live (name-phrase checked against a job detail to rule out
# same-slug impostors). A dead tenant or URL is parked in
# sourcing/parked_boards.csv by a reviewed change, never deleted.
# By design no company is fetched by both this source and ats.py — URL dedup
# in main.py stays as the backstop (PRD §2.7).
#
# Entries parked in sourcing/parked_boards.csv are skipped: a career URL as
# ats=careers_page keyed by company name, a Workday tenant as ats=workday keyed
# by "tenant:site". tools/probe_parked_boards.py re-probes them monthly.

import requests
import yaml
from bs4 import BeautifulSoup
from pathlib import Path
import time
import profile_loader
from sheet_dates import normalize_posted
from sources.ats import BoardResult, error_kind, title_passes_for, _strip_html, _work_model

COMPANIES_YAML = Path(__file__).resolve().parent.parent / "data" / "companies.yaml"


def load_pinned(path=COMPANIES_YAML):
    """(Workday tenants {company: {host, tenant, site}}, careers pages
    {company: url}) from the pinned: section of data/companies.yaml."""
    with Path(path).open() as fh:
        pinned = (yaml.safe_load(fh) or {}).get("pinned") or {}
    return dict(pinned.get("workday") or {}), dict(pinned.get("careers_pages") or {})


WORKDAY_TENANTS, DREAM_COMPANY_CAREER_URLS = load_pinned()

# PUB-04: the search and link terms are the profile's roles.watchlist_gate_terms.
_GATE_TERMS = profile_loader.active()["roles"]["watchlist_gate_terms"]
# Workday CXS search terms — full-text queries that surface leadership roles;
# results still pass the title pre-filter before being kept.
WORKDAY_SEARCH_TERMS = list(_GATE_TERMS["workday_search_terms"])
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_PAGES = 10  # per search term, safety cap

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Careers-page links: kept when the link text contains a seniority term; a
# function term only tags the role "IT" rather than "other".
SENIORITY_KEYWORDS = list(_GATE_TERMS["careers_page_seniority"])
IT_KEYWORDS = list(_GATE_TERMS["careers_page_function"])


def _parked_keys():
    """(ats, board_token) of every parked entry. Imported lazily: startups imports ats."""
    from sources.startups import read_parked
    return set(read_parked())


def workday_key(cfg):
    return ("workday", f"{cfg['tenant']}:{cfg['site']}")


def careers_key(company):
    return ("careers_page", company)


def _unresolved_companies():
    """Names from the unresolved: bucket of data/companies.yaml."""
    with COMPANIES_YAML.open() as fh:
        data = yaml.safe_load(fh) or {}
    return [e["name"] for e in (data.get("unresolved") or []) if e.get("name")]


def _workday_location(info):
    """Human-readable location, with the country name attached.

    Workday's own `location` field is frequently a bare internal facility
    code with no city or country in it at all (Vantage Data Centers' Dublin
    site posts as "DUB11") — location.py's international check has nothing to
    match, so the role passes as domestic (FIL-08). The country name Workday
    never puts in `location` lives in a separate `country.descriptor` field on
    the same posting; append it whenever it is not already part of the string.
    """
    location = info.get("location", "") or ""
    country = ((info.get("country") or {}).get("descriptor") or "").strip()
    # Only non-US countries actually change the classification (location.py's
    # US-signal check already treats a spelled-out US state/city as domestic
    # by default); skip "United States" so domestic postings stay uncluttered.
    if country and country.lower() != "united states" and country.lower() not in location.lower():
        return f"{location}, {country}" if location else country
    return location


def _workday_detail(base, tenant, site, external_path):
    """Fetch one posting's detail JSON: (description, location, work_model_hint, start_date).

    start_date is Workday's startDate, the posting's first-posted date (the list's
    postedOn is only relative text such as "Posted 30+ Days Ago")."""
    try:
        resp = requests.get(f"{base}/wday/cxs/{tenant}/{site}{external_path}",
                            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        info = resp.json().get("jobPostingInfo") or {}
        return (
            _strip_html(info.get("jobDescription", "")),
            _workday_location(info),
            (info.get("remoteType") or "").lower(),
            info.get("startDate") or "",
        )
    except Exception:
        return "", "", "", ""


def _fetch_workday(company, cfg, source="watchlist"):
    """Pull leadership roles from a Workday tenant via the CXS JSON endpoint.

    Returns a BoardResult. If any search request fails, the board is failed for
    this run: .error is set and no rows are returned. Never raises.
    """
    base = f"https://{cfg['host']}"
    tenant, site = cfg["tenant"], cfg["site"]
    jobs_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    seen_paths = set()
    postings = set()
    jobs = []

    for term in WORKDAY_SEARCH_TERMS:
        for page in range(WORKDAY_MAX_PAGES):
            body = {"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
                    "offset": page * WORKDAY_PAGE_SIZE, "searchText": term}
            try:
                resp = requests.post(jobs_url, json=body, headers=HEADERS, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"[Watchlist] Workday error for {company} ({term!r} p{page}) "
                      f"[{error_kind(e)}]: {e} — board failed for this run")
                return BoardResult(postings=len(postings), error=e)
            page_postings = data.get("jobPostings") or []
            for p in page_postings:
                path = p.get("externalPath", "")
                title = p.get("title", "")
                if path:
                    postings.add(path)
                if not path or path in seen_paths or not title_passes_for(source, title):
                    continue
                seen_paths.add(path)
                description, detail_loc, remote_type, start_date = _workday_detail(base, tenant, site, path)
                date_posted, posted_precision = normalize_posted(start_date)
                location = detail_loc or p.get("locationsText", "")
                work_model = "remote" if "remote" in remote_type else _work_model(location)
                jobs.append({
                    "title": title,
                    "company": company,
                    "location": location,
                    "work_model": work_model,
                    "comp": "",
                    "apply_link": f"{base}/en-US/{site}{path}",
                    "description": description,
                    "date_posted": date_posted,
                    "posted_precision": posted_precision,
                    "ats": "workday",
                    "source": source,
                    "watchlist_role_type": "IT",
                    "is_dream_company": True,
                })
                time.sleep(0.2)
            if len(page_postings) < WORKDAY_PAGE_SIZE:
                break
            time.sleep(0.3)
    return BoardResult(jobs, postings=len(postings))


def fetch_workday(company, host, tenant, site, source="ats"):
    """Fetch a resolved Workday board by host/tenant/site.

    Public entry point for sources/ats.py: a company resolved to Workday has no
    JSON board to GET, so it comes through the same CXS search path used for
    unresolved dream companies with a known tenant.
    """
    return _fetch_workday(company, {"host": host, "tenant": tenant, "site": site},
                          source=source)


def fetch_all():
    jobs = []
    unresolved = _unresolved_companies()
    parked = _parked_keys()
    parked_names = [c for c in unresolved
                    if (c in WORKDAY_TENANTS and workday_key(WORKDAY_TENANTS[c]) in parked)
                    or (c not in WORKDAY_TENANTS and careers_key(c) in parked)]
    workday = [c for c in unresolved if c in WORKDAY_TENANTS and c not in parked_names]
    scraped = [c for c in unresolved if c not in WORKDAY_TENANTS and c not in parked_names
               and DREAM_COMPANY_CAREER_URLS.get(c)]
    uncovered = [c for c in unresolved
                 if c not in workday and c not in scraped and c not in parked_names]
    if parked_names:
        print(f"[Watchlist] {len(parked_names)} parked, not fetched — see "
              f"sourcing/parked_boards.csv: {', '.join(parked_names)}")
    if uncovered:
        print(f"[Watchlist] {len(uncovered)} unresolved companies have neither a Workday "
              f"tenant nor a career URL (not monitored): {', '.join(uncovered)}")

    failed = 0
    for company in workday:
        found = _fetch_workday(company, WORKDAY_TENANTS[company])
        if found.error is not None:
            failed += 1
            print(f"[Watchlist] Workday {company}: FAILED [{error_kind(found.error)}] — no rows this run")
        else:
            print(f"[Watchlist] Workday {company}: {found.postings} postings fetched, "
                  f"{len(found)} title-matched roles")
            jobs.extend(found)
        time.sleep(1)

    for company in scraped:
        found = _check_company(company, DREAM_COMPANY_CAREER_URLS[company])
        jobs.extend(found)
        time.sleep(2)

    print(f"[Watchlist] Found {len(jobs)} roles ({len(workday)} Workday tenants, "
          f"{failed} failed; {len(scraped)} scraped career pages)")
    return jobs


def _check_company(company, careers_url):
    jobs = []
    try:
        resp = requests.get(careers_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        base = "/".join(careers_url.split("/")[:3])

        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            href = link.get("href", "")

            if not text or len(text) < 5 or len(text) > 120:
                continue

            text_lower = text.lower()
            is_senior = any(kw in text_lower for kw in SENIORITY_KEYWORDS)
            is_it = any(kw in text_lower for kw in IT_KEYWORDS)

            if is_senior:
                full_url = href if href.startswith("http") else f"{base}{href}"
                jobs.append({
                    "title": text,
                    "company": company,
                    "location": "",
                    "work_model": "",
                    "comp": "",
                    "apply_link": full_url,
                    "description": "",
                    "date_posted": "",  # a scraped career page has no reliable date
                    "posted_precision": "",
                    "source": "watchlist",
                    "watchlist_role_type": "IT" if is_it else "other",
                    "is_dream_company": True,
                })
    except Exception as e:
        print(f"[Watchlist] Error checking {company}: {e}")
    return jobs
