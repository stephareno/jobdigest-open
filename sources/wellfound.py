# ============================================================
# sources/wellfound.py — Fetches jobs from Wellfound (AngelList)
# ============================================================

import requests
from bs4 import BeautifulSoup
import time
from config import title_matches

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SEARCH_URLS = [
    "https://wellfound.com/jobs?role=VP+of+IT&remote=true",
    "https://wellfound.com/jobs?role=Director+of+IT&remote=true",
    "https://wellfound.com/jobs?role=Head+of+IT",
]


def fetch_all():
    jobs = []
    seen_urls = set()

    for url in SEARCH_URLS:
        batch = _fetch_page(url)
        for job in batch:
            job_url = job.get("apply_link", "")
            if job_url and job_url not in seen_urls:
                seen_urls.add(job_url)
                jobs.append(job)
        time.sleep(1.5)

    matching = [j for j in jobs if _title_matches(j.get("title", ""))]
    print(f"[Wellfound] Fetched {len(matching)} matching jobs")
    return matching


def _fetch_page(url):
    jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("[data-test='StartupResult']") or soup.select("div[class*='job']") or soup.select("article")

        for card in cards[:40]:
            job = _parse_card(card)
            if job:
                jobs.append(job)
    except Exception as e:
        print(f"[Wellfound] Error fetching {url}: {e}")
    return jobs


def _parse_card(card):
    try:
        title_el = card.select_one("h2, h3, a[href*='/jobs/']")
        company_el = card.select_one("[class*='company'], [class*='startup']")
        location_el = card.select_one("[class*='location']")
        link_el = card.select_one("a[href*='/jobs/']") or card.select_one("a")

        title = title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        href = link_el.get("href", "") if link_el else ""

        if not title or not href:
            return None

        url = href if href.startswith("http") else f"https://wellfound.com{href}"
        text = (title + " " + location).lower()
        work_model = "Remote" if "remote" in text else "Hybrid" if "hybrid" in text else "In-office"

        return {
            "title": title, "company": company, "location": location,
            "work_model": work_model, "comp": "", "apply_link": url,
            "description": "", "date_posted": "", "source": "wellfound",
        }
    except Exception:
        return None


def _title_matches(title):
    # Shared PRD §3 gate — fixed phrases plus every "Head of <function>" variant.
    return title_matches(title)
