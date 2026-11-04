# ============================================================
# sources/ats.py — Fetches dream-company jobs from ATS board APIs
# ============================================================
#
# Reads the resolved boards from data/companies.yaml (written by
# tools/resolve_ats.py) and pulls every board's public JSON API daily:
# Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Workable, Recruitee,
# BambooHR, Breezy, Teamtailor, Personio, Oracle Cloud HCM, and (SRC-13) ADP
# Workforce Now, UKG Ready, Gem, Comeet, Manatal, JobScore. Unresolved companies
# (Workday, custom sites) are covered by sources/watchlist.py instead.
#
# A broader title pre-filter (including Security / InfoSec /
# Infrastructure) runs here as a cost gate before scoring — pure-infosec
# roles still take their −2 at scoring per PRD §4. Location filtering
# happens post-fetch in main.py like every other source (PRD §3).

import re
import time
import html as html_lib

import requests
import yaml
from bs4 import BeautifulSoup
from pathlib import Path
from urllib.parse import urlsplit

from sheet_dates import normalize_posted

COMPANIES_YAML = Path(__file__).resolve().parent.parent / "data" / "companies.yaml"

# A real browser User-Agent and Accept headers. Breezy and Teamtailor answer
# 403 to GitHub Actions runner IPs (every board attempted on 2026-09-10) while
# serving the same JSON endpoints to a laptop — bot detection, not dead boards.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 15
BOARD_DELAY = 0.2  # courtesy delay between boards

# Retry with backoff for failures that can be transient: bot-detection 403s,
# rate limits, server errors and network timeouts. A 404 or 401 is not
# retried — it means the slug moved or the board is private (see
# sourcing/parked_boards.csv).
RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}
RETRY_BACKOFF_SECS = (2, 5)  # waits before attempts 2 and 3


def get_with_retry(url, **kwargs):
    """GET with up to len(RETRY_BACKOFF_SECS) retries; raises on final failure."""
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", TIMEOUT)
    for attempt, wait in enumerate((*RETRY_BACKOFF_SECS, None), start=1):
        try:
            resp = requests.get(url, **kwargs)
            if resp.status_code not in RETRY_STATUSES or wait is None:
                resp.raise_for_status()
                return resp
        except (requests.ConnectionError, requests.Timeout):
            if wait is None:
                raise
        time.sleep(wait)

# Cost gate: the shared PRD §3 title gate (config.title_gate, terms from the
# profile's roles section), applied while parsing so Rippling/BambooHR/Workday
# detail requests are only spent on titles that will be scored. Bare
# "enterprise", "digital" or "corporate" never pass on their own (the profile's
# roles.function_terms_never_alone) — they admitted dream-company sales and
# marketing roles ("Head of Enterprise Sales") that the scorer then rejected.
from config import TITLE_GATED_SOURCES, source_key, title_gate as _title_gate, watchlist_title_gate


def _title_passes(title):
    return _title_gate(title)


# The pre-2026-09-11 gate, kept only for sources exempt from the shared gate
# (roles.title_gated_sources); its terms are roles.watchlist_gate_terms.
# Watchlist stays on it: under the shared gate it would have lost a proven
# score-8 role, Nvidia's "Senior Director, Enterprise Networking".
def _legacy_title_passes(title):
    return watchlist_title_gate(title)


def title_passes_for(source, title):
    """The gate a given source (its key; an older display name is converted)
    applies while parsing."""
    return _title_passes(title) if source_key(source) in TITLE_GATED_SOURCES else _legacy_title_passes(title)


def _strip_html(text):
    if not text:
        return ""
    return BeautifulSoup(html_lib.unescape(text), "html.parser").get_text(" ", strip=True)


def _work_model(location_text, remote_flag=None, workplace_type=None):
    """Best-effort work model; empty string means unknown (scorer decides)."""
    if workplace_type:
        return {"remote": "remote", "hybrid": "hybrid", "onsite": "in-office", "on-site": "in-office"}.get(
            workplace_type.lower(), ""
        )
    if remote_flag:
        return "remote"
    if "remote" in (location_text or "").lower():
        return "remote"
    return ""


def _job(company, title, location, apply_link, description="", date_posted="",
         remote_flag=None, workplace_type=None):
    """date_posted is the vendor's raw first-posted value; it is stored whole,
    normalized to UTC ISO, with posted_precision "time" or "date" (DEL-05)."""
    date_posted, posted_precision = normalize_posted(date_posted)
    return {
        "title": title,
        "company": company,
        "location": location or "",
        "work_model": _work_model(location, remote_flag, workplace_type),
        "comp": "",
        "apply_link": apply_link,
        "description": description,
        "date_posted": date_posted,
        "posted_precision": posted_precision,
        "source": "ats",
        "is_dream_company": True,
    }


def _parse_greenhouse(company, data):
    for j in data.get("jobs") or []:
        title = j.get("title", "")
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""),
            description=_strip_html(j.get("content", "")),
            # first_published, not updated_at: any edit bumps updated_at.
            date_posted=j.get("first_published") or "",
        )


def _parse_lever(company, data):
    for j in data or []:
        title = j.get("text", "")
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            (j.get("categories") or {}).get("location", ""),
            j.get("hostedUrl", ""),
            description=_strip_html(j.get("description", "")),
            date_posted=j.get("createdAt"),  # epoch milliseconds
            workplace_type=j.get("workplaceType"),
        )


RIPPLING_DETAIL = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}"


def _rippling_detail(slug, uuid):
    """Fetch one Rippling posting's body and createdOn: (description, created_on).

    Rippling's board endpoint returns titles only, so the JD text needs a second
    request per posting. Only called for postings that already cleared the title
    gate, so this stays a handful of requests per board. Returns ("", "") on
    failure — a missing description should not drop an otherwise valid role.
    """
    try:
        r = requests.get(RIPPLING_DETAIL.format(slug=slug, uuid=uuid),
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        detail = r.json()
    except Exception as e:
        print(f"[ATS] Rippling detail fetch failed for {slug}/{uuid}: {e}")
        return "", ""
    desc = detail.get("description")
    # description is a dict of named HTML sections (company, role, benefits...).
    if isinstance(desc, dict):
        text = _strip_html(" ".join(str(v) for v in desc.values()))
    else:
        text = _strip_html(str(desc or ""))
    return text, detail.get("createdOn") or ""


def _parse_rippling(company, data, slug):
    for j in data or []:
        title = j.get("name", "")
        if not _title_passes(title):
            continue
        uuid = j.get("uuid", "")
        description, created_on = _rippling_detail(slug, uuid) if uuid else ("", "")
        yield _job(
            company, title,
            (j.get("workLocation") or {}).get("label", ""),
            j.get("url", ""),
            description=description,
            date_posted=created_on,
        )
        time.sleep(BOARD_DELAY)


def _parse_ashby(company, data):
    for j in data.get("jobs") or []:
        title = j.get("title", "")
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            j.get("location", ""),
            j.get("jobUrl") or j.get("applyUrl", ""),
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")),
            date_posted=j.get("publishedAt") or "",
            remote_flag=j.get("isRemote"),
        )


def _parse_smartrecruiters(company, data, slug):
    for j in data.get("content") or []:
        title = j.get("name", "")
        if not _title_passes(title):
            continue
        loc = j.get("location") or {}
        location = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
        yield _job(
            company, title, location,
            f"https://jobs.smartrecruiters.com/{slug}/{j.get('id', '')}",
            date_posted=j.get("releasedDate") or "",
            remote_flag=loc.get("remote"),
        )


# --- providers added Aug 2026 -------------------------------------------------
# Each has a matching entry in tools/resolve_ats.py. A resolver entry without a
# parser here means the daily fetch silently skips the company, so the two must
# always be added together.


def _loc(*parts):
    """Join non-empty location fragments into one comma-separated string."""
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _parse_workable(company, data):
    for j in data.get("jobs") or []:
        title = j.get("title", "")
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            _loc(j.get("city"), j.get("state"), j.get("country")),
            j.get("shortlink") or j.get("url", ""),
            description=_strip_html(j.get("description", "")),
            # published_on only: created_at is when the draft was made.
            date_posted=j.get("published_on") or "",
            remote_flag=j.get("telecommuting"),
        )


def _parse_recruitee(company, data):
    for j in data.get("offers") or []:
        title = j.get("title", "")
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            j.get("location") or _loc(j.get("city"), j.get("state_name"), j.get("country")),
            j.get("careers_url") or j.get("careers_apply_url", ""),
            description=_strip_html(j.get("description", "")),
            # published_at, not created_at: offers are often drafted long before.
            date_posted=j.get("published_at") or "",
            workplace_type="hybrid" if j.get("hybrid") else None,
        )


BAMBOOHR_DETAIL = "https://{slug}.bamboohr.com/careers/{job_id}/detail"


def _bamboohr_detail(slug, job_id):
    """Fetch one BambooHR posting: (description, share_url, date_posted).
    The list endpoint carries neither the description nor the posted date."""
    try:
        r = requests.get(BAMBOOHR_DETAIL.format(slug=slug, job_id=job_id),
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        opening = (r.json().get("result") or {}).get("jobOpening") or {}
    except Exception as e:
        print(f"[ATS] BambooHR detail fetch failed for {slug}/{job_id}: {e}")
        return "", "", ""
    return (_strip_html(opening.get("description", "")), opening.get("jobOpeningShareUrl", ""),
            opening.get("datePosted") or "")


def _parse_bamboohr(company, data, slug):
    for j in data.get("result") or []:
        title = j.get("jobOpeningName", "")
        if not _title_passes(title):
            continue
        job_id = j.get("id", "")
        ats_loc = j.get("atsLocation") or {}
        description, share_url, posted = _bamboohr_detail(slug, job_id) if job_id else ("", "", "")
        yield _job(
            company, title,
            _loc(ats_loc.get("city"), ats_loc.get("state"), ats_loc.get("country")),
            share_url or f"https://{slug}.bamboohr.com/careers/{job_id}",
            description=description,
            date_posted=posted,
            remote_flag=j.get("isRemote"),
        )
        time.sleep(BOARD_DELAY)


def _parse_breezy(company, data):
    # Breezy's public JSON carries no description; the scorer sees title +
    # location only for these. Logged rather than silently dropped.
    for j in data or []:
        title = j.get("name", "")
        if not _title_passes(title):
            continue
        loc = j.get("location") or {}
        yield _job(
            company, title,
            _loc(loc.get("city"), (loc.get("country") or {}).get("name")),
            j.get("url", ""),
            date_posted=j.get("published_date") or "",
            remote_flag=(loc.get("is_remote") if isinstance(loc, dict) else None),
        )


def _parse_teamtailor(company, data):
    for j in data.get("items") or []:
        title = j.get("title", "")
        if not _title_passes(title):
            continue
        # Location lives only in the embedded schema.org JobPosting blob.
        posting = j.get("_jobposting") or {}
        job_loc = posting.get("jobLocation") or {}
        if isinstance(job_loc, list):
            job_loc = job_loc[0] if job_loc else {}
        address = (job_loc.get("address") or {}) if isinstance(job_loc, dict) else {}
        yield _job(
            company, title,
            _loc(address.get("addressLocality"), address.get("addressRegion"),
                 address.get("addressCountry")),
            j.get("url", ""),
            description=_strip_html(j.get("content_html", "")),
            date_posted=j.get("date_published") or "",
        )


def _parse_personio(company, data, slug):
    for j in data or []:
        title = j.get("name", "")
        if not _title_passes(title):
            continue
        offices = j.get("offices") or ([j["office"]] if j.get("office") else [])
        yield _job(
            company, title,
            ", ".join(str(o) for o in offices),
            f"https://{slug}.jobs.personio.de/job/{j.get('id', '')}",
            description=_strip_html(j.get("description", "")),
        )


# Oracle Cloud HCM: slug is "host:siteNumber" — the token a careers page yields.
ORACLE_WORKPLACE = {
    "ORA_REMOTE": "remote", "ORA_ON_SITE": "onsite", "ORA_HYBRID": "hybrid",
}


def _parse_oracle_hcm(company, data, slug):
    # Oracle's public list endpoint leaves the description fields empty and the
    # detail endpoint rejects unauthenticated calls, so these roles reach the
    # scorer as title + location only.
    host, _, site = slug.partition(":")
    for item in data.get("items") or []:
        for j in item.get("requisitionList") or []:
            title = j.get("Title", "")
            if not _title_passes(title):
                continue
            description = _strip_html(" ".join(str(j.get(k) or "") for k in (
                "ShortDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))
            yield _job(
                company, title,
                j.get("PrimaryLocation") or j.get("PrimaryLocationCountry", ""),
                f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id', '')}",
                description=description,
                date_posted=j.get("PostedDate") or "",
                workplace_type=ORACLE_WORKPLACE.get(j.get("WorkplaceTypeCode")),
            )


# --- providers added Sep 2026 (SRC-13) -----------------------------------------
# Found only on careers pages (tools/resolve_ats.py PAGE_ONLY_PROBES). Each
# parser was written against a live payload saved in tests/fixtures/ats/.

ADP_DETAIL = ("https://workforcenow.adp.com/mascsr/default/careercenter/public/events/"
              "staffing/v1/job-requisitions/{item_id}?cid={cid}&timeStamp=0&lang=en_US&locale=en_US")
ADP_APPLY = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
             "?cid={cid}&jobId={job_id}&lang=en_US")


def _adp_field(job, group, code):
    """One named value out of ADP's customFieldGroup (e.g. stringFields / ExternalJobID)."""
    for field in (job.get("customFieldGroup") or {}).get(group) or []:
        if ((field.get("nameCode") or {}).get("codeValue")) == code:
            return field.get("stringValue") or field.get("dateValue") or ""
    return ""


def _adp_description(cid, item_id):
    """ADP's list endpoint carries no description; fetch it for gated titles only."""
    try:
        r = requests.get(ADP_DETAIL.format(item_id=item_id, cid=cid), headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return _strip_html(r.json().get("requisitionDescription", ""))
    except Exception as e:
        print(f"[ATS] ADP detail fetch failed for {cid}/{item_id}: {e}")
        return ""


def _parse_adp(company, data, slug):
    for j in data.get("jobRequisitions") or []:
        title = (j.get("requisitionTitle") or "").strip()
        if not _title_passes(title):
            continue
        locations = [((loc.get("nameCode") or {}).get("shortName") or "").strip()
                     for loc in j.get("requisitionLocations") or []]
        job_id = _adp_field(j, "stringFields", "ExternalJobID")
        item_id = j.get("itemID", "")
        yield _job(
            company, title,
            "; ".join(l for l in locations if l),
            ADP_APPLY.format(cid=slug, job_id=job_id),
            description=_adp_description(slug, item_id) if item_id else "",
            date_posted=j.get("postDate") or "",
        )
        time.sleep(BOARD_DELAY)


def _parse_ukg_ready(company, data, slug):
    host, _, company_id = slug.partition(":")
    for j in data.get("job_requisitions") or []:
        title = j.get("job_title", "")
        if not _title_passes(title):
            continue
        loc = j.get("location") or {}
        yield _job(
            company, title,
            _loc(loc.get("city"), loc.get("state"), loc.get("country")),
            f"https://{host}/ta/{company_id}.careers?ShowJob={j.get('id', '')}",
            description=_strip_html(j.get("job_description", "")),
            remote_flag=j.get("is_remote_job"),
        )


GEM_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "in_office": "onsite", "onsite": "onsite"}


def _parse_gem(company, data):
    for j in data or []:
        title = (j.get("title") or "").strip()
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""),
            description=j.get("content_plain") or _strip_html(j.get("content", "")),
            date_posted=j.get("first_published_at") or j.get("created_at") or "",
            workplace_type=GEM_WORKPLACE.get((j.get("location_type") or "").lower()),
        )


def _parse_comeet(company, data):
    for j in data or []:
        title = (j.get("name") or "").strip()
        if not _title_passes(title):
            continue
        loc = j.get("location") or {}
        details = j.get("details") or []
        description = " ".join(str(d.get("value") or "") for d in details if isinstance(d, dict))
        yield _job(
            company, title,
            _loc(loc.get("city"), loc.get("state"), loc.get("country")) or loc.get("name", ""),
            j.get("url_active_page") or j.get("url_comeet_hosted_page", ""),
            description=_strip_html(description),
            # No date_posted: Comeet exposes only time_updated, which moves on every
            # edit, and no first-published field (checked on 131 live postings,
            # 17 Sep 2026). An edited posting must not read as fresh (DEL-05).
            remote_flag=loc.get("is_remote"),
            workplace_type=j.get("workplace_type"),  # "Remote" / "Hybrid" / "On-site"
        )


def _parse_manatal(company, data, slug):
    for j in data.get("results") or []:
        title = (j.get("position_name") or "").strip()
        if not _title_passes(title):
            continue
        yield _job(
            company, title,
            j.get("location_display") or _loc(j.get("city"), j.get("state"), j.get("country")),
            f"https://www.careers-page.com/{slug}/job/{j.get('hash', '')}",
            description=_strip_html(j.get("description", "")),
            remote_flag=j.get("is_remote"),
        )


def _parse_jobscore(company, data):
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        if not _title_passes(title):
            continue
        remote_text = (j.get("remote") or "").lower()
        yield _job(
            company, title,
            j.get("location") or _loc(j.get("city"), j.get("state"), j.get("country")),
            j.get("detail_url") or j.get("apply_url", ""),
            description=_strip_html(j.get("description", "")),
            date_posted=j.get("opened_date") or j.get("created_on") or "",
            workplace_type=("hybrid" if remote_text.startswith("hybrid")
                            else "remote" if remote_text.startswith("remote") else None),
        )


class BoardResult(list):
    """One board's title-matched jobs, plus the run-log facts about the fetch.

    postings: postings the board returned before the title gate (None if unknown).
    error: the exception if a request failed. A failed board yields no rows and
    is reported as failed for this run; it is never parked automatically.
    """

    def __init__(self, jobs=(), postings=None, error=None):
        super().__init__(jobs)
        self.postings = postings
        self.error = error


def posting_count(ats, data):
    """Postings in a raw board payload, before the title gate."""
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, dict):
        return 0
    if ats == "oracle_hcm":
        return sum(len(item.get("requisitionList") or []) for item in data.get("items") or [])
    key = {"smartrecruiters": "content", "recruitee": "offers", "bamboohr": "result",
           "teamtailor": "items", "adp": "jobRequisitions", "ukg_ready": "job_requisitions",
           "manatal": "results"}.get(ats, "jobs")
    return len(data.get(key) or [])


def _load_boards():
    with COMPANIES_YAML.open() as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("companies") or []


def parse_board(company, ats, slug, data):
    """Dispatch a fetched board payload to its parser. None means unsupported.
    Every job is tagged with its vendor as "ats", which config.NO_FRESH_FLAG_VENDORS
    is checked against (DEL-05)."""
    batch = _dispatch_board(company, ats, slug, data)
    return None if batch is None else _tag_vendor(batch, ats)


def _tag_vendor(batch, ats):
    for job in batch:
        job["ats"] = ats
        yield job


def _dispatch_board(company, ats, slug, data):
    if ats == "greenhouse":
        return _parse_greenhouse(company, data)
    if ats == "lever":
        return _parse_lever(company, data)
    if ats == "ashby":
        return _parse_ashby(company, data)
    if ats == "smartrecruiters":
        return _parse_smartrecruiters(company, data, slug)
    if ats == "rippling":
        return _parse_rippling(company, data, slug)
    if ats == "workable":
        return _parse_workable(company, data)
    if ats == "recruitee":
        return _parse_recruitee(company, data)
    if ats == "bamboohr":
        return _parse_bamboohr(company, data, slug)
    if ats == "breezy":
        return _parse_breezy(company, data)
    if ats == "teamtailor":
        return _parse_teamtailor(company, data)
    if ats == "personio":
        return _parse_personio(company, data, slug)
    if ats == "oracle_hcm":
        return _parse_oracle_hcm(company, data, slug)
    if ats == "adp":
        return _parse_adp(company, data, slug)
    if ats == "ukg_ready":
        return _parse_ukg_ready(company, data, slug)
    if ats == "gem":
        return _parse_gem(company, data)
    if ats == "comeet":
        return _parse_comeet(company, data)
    if ats == "manatal":
        return _parse_manatal(company, data, slug)
    if ats == "jobscore":
        return _parse_jobscore(company, data)
    return None


def _fetch_workday_board(company, slug, board_url, source="ats"):
    """Workday has no static JSON board; its postings come from the CXS search
    endpoint that sources/watchlist.py already speaks.

    Routing here is what keeps a Workday-resolved company from falling into the
    "Unknown ATS" skip below. The slug is "tenant:site"; the host has to come
    from the board URL, because Workday shards tenants across wd1..wd12.
    """
    # Imported inside the call: watchlist imports this module at import time,
    # so a module-level import here would be circular.
    from sources.watchlist import fetch_workday

    tenant, _, site = (slug or "").partition(":")
    url = board_url if "://" in (board_url or "") else f"https://{board_url}"
    host = urlsplit(url).netloc
    if not (host and tenant and site):
        print(f"[ATS] Workday board for {company} missing host/tenant/site — skipping")
        return BoardResult()
    return fetch_workday(company, host, tenant, site, source=source)


def fetch_one_board(company, ats, slug, url, source="ats"):
    """Fetch and parse one board. Returns a list of job dicts (may be empty).

    Shared by the dream tier (data/companies.yaml) and the startup tier
    (sourcing/startups_ats_resolved.csv) so both speak every ATS identically.
    Returns a BoardResult. JSON-board transport errors raise; callers count
    them. Workday search errors never raise: they come back as a BoardResult
    with .error set and no rows, and callers report the board as failed.
    """
    if ats == "workday":
        return _fetch_workday_board(company, slug, url, source=source)
    resp = get_with_retry(url)
    data = resp.json()
    batch = parse_board(company, ats, slug, data)
    if batch is None:
        print(f"[ATS] Unknown ATS {ats!r} for {company} — skipping")
        return BoardResult()
    return BoardResult(batch, postings=posting_count(ats, data))


def fetch_all():
    jobs = []
    boards = _load_boards()
    errors = 0
    per_vendor = {}  # ats -> [boards, ok, jobs, postings]

    for entry in boards:
        company, ats, slug = entry.get("name"), entry.get("ats"), entry.get("slug")
        url = entry.get("board_url")
        if not (company and ats and url):
            continue
        tally = per_vendor.setdefault(ats, [0, 0, 0, 0])
        tally[0] += 1
        try:
            batch = fetch_one_board(company, ats, slug, url)
            if batch.error is not None:
                errors += 1
                print(f"[ATS] FAILED {company} ({ats}/{slug}) [{error_kind(batch.error)}]: {batch.error}")
            else:
                jobs.extend(batch)
                tally[1] += 1
                tally[2] += len(batch)
                tally[3] += batch.postings or 0
        except Exception as e:
            errors += 1
            status = getattr(getattr(e, "response", None), "status_code", None)
            print(f"[ATS] FAILED {company} ({ats}/{slug}) [{status or type(e).__name__}]: {e}")
        time.sleep(BOARD_DELAY)

    print(f"[ATS] {len(jobs)} title-matched roles across {len(boards)} boards ({errors} board errors)")
    for ats, (n, ok, found, _postings) in sorted(per_vendor.items(), key=lambda kv: -kv[1][0]):
        print(f"[ATS] per vendor: {ats:<16} boards={n:<4} ok={ok:<4} failed={n - ok:<3} jobs={found}")
    for ats, (_n, _ok, found, postings) in sorted(per_vendor.items(), key=lambda kv: -kv[1][0]):
        print(postings_line("[ATS]", ats, postings, found))
    return jobs


def error_kind(exc):
    """Short label for a failed request: HTTP status if there is one, else the type."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return str(status) if status else type(exc).__name__


def postings_line(prefix, ats, postings, passed):
    """One run-log line per vendor: postings fetched vs postings passing the title gate."""
    note = " (Workday: postings from the director / vice president / head of searches)" if ats == "workday" else ""
    return f"{prefix} postings: {ats:<16} fetched={postings:<6} passed title gate={passed}{note}"
