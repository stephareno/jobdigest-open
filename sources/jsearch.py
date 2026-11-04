# ============================================================
# sources/jsearch.py — Fetches jobs from JSearch (RapidAPI)
# ============================================================

import os
import requests
import time

import profile_loader
from url_resolver import best_link
from sheet_dates import normalize_posted

JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
}

# Results pass the shared PRD §3 title gate in main.add() (config.title_gate)
# before scoring. The query list (profile sources.jsearch.queries, PUB-05)
# still decides what JSearch can surface: "Head of IT" is what surfaces the
# "Head of ..." family. Each entry is one RapidAPI call per run.
PRIORITY_KEYWORDS = list(profile_loader.active()["sources"]["jsearch"]["queries"])


def fetch_all():
    jobs = []
    seen_urls = set()

    for keyword in PRIORITY_KEYWORDS:
        batch = _fetch(keyword)
        for job in batch:
            url = job.get("apply_link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                jobs.append(job)
        time.sleep(1)

    print(f"[JSearch] Fetched {len(jobs)} unique jobs")
    return jobs


def _fetch(keyword):
    params = {
        "query": f"{keyword} United States",
        "page": "1",
        "num_pages": "1",
        "date_posted": "week",
        "employment_types": "FULLTIME",
    }
    try:
        resp = requests.get(JSEARCH_URL, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return [_normalize(r) for r in data.get("data", [])]
    except Exception as e:
        print(f"[JSearch] Error fetching '{keyword}': {e}")
        return []


def _normalize(r):
    salary_min = r.get("job_min_salary")
    salary_max = r.get("job_max_salary")
    comp = ""
    if salary_min and salary_max:
        comp = f"${int(salary_min):,} – ${int(salary_max):,}"
    elif salary_min:
        comp = f"${int(salary_min):,}+"

    is_remote = r.get("job_is_remote", False)
    location = r.get("job_city") or ""
    if r.get("job_state"):
        location += f", {r.get('job_state')}"
    work_model = "Remote" if is_remote else _infer_work_model(r.get("job_title") or "", r.get("job_description") or "")
    posted, precision = normalize_posted(r.get("job_posted_at_datetime_utc"), date_only=True)

    return {
        "title": r.get("job_title") or "",
        "company": r.get("employer_name") or "",
        "location": location,
        "work_model": work_model,
        "comp": comp,
        "apply_link": _apply_link(r),
        "description": r.get("job_description") or "",
        # JSearch stamps every posting at midnight UTC: a date, not a time.
        "date_posted": posted,
        "posted_precision": precision,
        "source": "jsearch",
    }


def _apply_link(r):
    """Best of job_apply_link and every apply_options link, by PRD §6 priority.

    job_apply_link is JSearch's first publisher, often LinkedIn or Jooble, even
    when the same posting lists the employer's own page in apply_options.
    """
    options = [o.get("apply_link") for o in (r.get("apply_options") or []) if isinstance(o, dict)]
    return best_link([r.get("job_apply_link") or ""] + options)


def _infer_work_model(title, description):
    text = (title + " " + description).lower()
    if "remote" in text:
        return "Remote"
    elif "hybrid" in text:
        return "Hybrid"
    return "In-office"
