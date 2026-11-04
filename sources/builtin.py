# ============================================================
# sources/builtin.py — Scrapes Built In for IT leadership roles
# ============================================================

import requests
from bs4 import BeautifulSoup
import time
from config import builtin_search_urls, title_matches
from sheet_dates import parse_relative_posted

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# PUB-05: profile sources.builtin.search_urls, or built from its
# title_queries and the home metro's Built In site (config.builtin_search_urls).
SEARCH_URLS = builtin_search_urls()


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
    print(f"[Built In] Fetched {len(matching)} matching jobs")
    return matching


def _fetch_page(url):
    jobs = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li[class*='job']") or soup.select("article") or soup.select("[data-id]")

        for card in cards[:50]:
            job = _parse_card(card)
            if job:
                jobs.append(job)
    except Exception as e:
        print(f"[Built In] Error fetching {url}: {e}")
    return jobs


def _parse_card(card):
    try:
        title_el = card.select_one("h2, h3, [class*='title']")
        company_el = card.select_one("[class*='company']")
        location_el = card.select_one("[class*='location']")
        link_el = card.select_one("a[href*='/job/']") or card.select_one("a")

        title = title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        href = link_el.get("href", "") if link_el else ""

        if not title or not href:
            return None

        url = href if href.startswith("http") else f"https://builtin.com{href}"
        # The card's relative age ("3 Hours Ago", "Yesterday"); "Reposted ..." gives nothing.
        date_posted, posted_precision = parse_relative_posted(card.get_text(" ", strip=True))

        text = (title + " " + location).lower()
        work_model = "Remote" if "remote" in text else "Hybrid" if "hybrid" in text else "In-office"

        return {
            "title": title, "company": company, "location": location,
            "work_model": work_model, "comp": "", "apply_link": url,
            "description": "", "date_posted": date_posted,
            "posted_precision": posted_precision, "source": "builtin",
        }
    except Exception:
        return None


def _title_matches(title):
    # Shared PRD §3 gate — fixed phrases plus every "Head of <function>" variant.
    return title_matches(title)
