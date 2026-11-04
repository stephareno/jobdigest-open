# ============================================================
# sources/adzuna.py — Fetches jobs from Adzuna API
# ============================================================

import os
import requests
import time
import profile_loader
from config import adzuna_locations
from sheet_dates import normalize_posted

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/us/search/1"
APP_ID = os.environ["ADZUNA_APP_ID"]
API_KEY = os.environ["ADZUNA_API_KEY"]

# PUB-05: profile sources.adzuna. One API call per query per location per run,
# so the product is a cost. Locations are derived from the location section
# when the profile leaves them out (config.adzuna_locations).
ADZUNA_QUERIES = list(profile_loader.active()["sources"]["adzuna"]["queries"])
LOCATIONS = adzuna_locations()


def fetch_all():
    jobs = []
    seen_urls = set()

    for keyword in ADZUNA_QUERIES:
        for location in LOCATIONS:
            batch = _fetch(keyword, location)
            for job in batch:
                url = job.get("apply_link", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    jobs.append(job)
            time.sleep(0.5)

    print(f"[Adzuna] Fetched {len(jobs)} unique jobs")
    return jobs


def _fetch(keyword, location, max_results=50):
    params = {
        "app_id": APP_ID,
        "app_key": API_KEY,
        "what": keyword,
        "where": location,
        "results_per_page": max_results,
        "max_days_old": 14,
        "full_time": 1,
        "sort_by": "date",
        "content-type": "application/json",
    }
    try:
        resp = requests.get(ADZUNA_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return [_normalize(r) for r in data.get("results", [])]
    except Exception as e:
        print(f"[Adzuna] Error fetching '{keyword}' in '{location}': {e}")
        return []


def _normalize(r):
    salary_min = r.get("salary_min")
    salary_max = r.get("salary_max")
    comp = ""
    if salary_min and salary_max:
        comp = f"${int(salary_min):,} – ${int(salary_max):,}"
    elif salary_min:
        comp = f"${int(salary_min):,}+"

    location = r.get("location", {}).get("display_name", "")
    work_model = _infer_work_model(r.get("title", ""), r.get("description", ""), location)
    posted, precision = normalize_posted(r.get("created"))

    return {
        "title": r.get("title", ""),
        "company": r.get("company", {}).get("display_name", ""),
        "location": location,
        "work_model": work_model,
        "comp": comp,
        # Adzuna's own salary estimate, not the employer's: shown, but never
        # read as stated comp for the SCO-08 comp floor.
        "comp_is_predicted": str(r.get("salary_is_predicted", "")) == "1",
        "apply_link": r.get("redirect_url", ""),
        "description": r.get("description", ""),
        "date_posted": posted,
        "posted_precision": precision,
        "source": "adzuna",
    }


def _infer_work_model(title, description, location):
    text = (title + " " + description + " " + location).lower()
    if "remote" in text:
        return "Remote"
    elif "hybrid" in text:
        return "Hybrid"
    return "In-office"
