# ============================================================
# sources/linkedin_email.py — Reads LinkedIn job alert emails
# ============================================================

import os
import imaplib
import email
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from config import GMAIL_IMAP_SERVER, GMAIL_IMAP_PORT
import profile_loader
from scorer import JD_CHARS

GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"].strip()
# The Gmail label LinkedIn alerts are filed under (profile sources.linkedin_label).
LINKEDIN_ALERT_FOLDER = profile_loader.active()["sources"]["linkedin_label"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].strip()

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def fetch_all():
    print("\n[LinkedIn Email DEBUG] ========== fetch_all() START ==========")
    job_ids = _collect_job_ids_from_email()

    print(f"\n[LinkedIn Email DEBUG] --- Step 2: URL construction ---")
    if not job_ids:
        print("[LinkedIn Email DEBUG] No job IDs found — returning empty list")
        return []

    urls = [f"https://www.linkedin.com/jobs/view/{jid}/" for jid in job_ids]
    for jid, url in zip(job_ids, urls):
        print(f"[LinkedIn Email DEBUG]   job_id={jid}  url={url}")

    print(f"\n[LinkedIn Email DEBUG] --- Step 3: seen_jobs filter ---")
    import state as _state
    unseen_ids = []
    for jid, url in zip(job_ids, urls):
        normalized = _state.normalize_url(url)
        seen = _state.is_seen(url)
        print(f"[LinkedIn Email DEBUG]   {jid}  normalized={normalized}  already_seen={seen}")
        if not seen:
            unseen_ids.append(jid)
    print(f"[LinkedIn Email DEBUG]   {len(job_ids)} total → {len(unseen_ids)} after seen_jobs filter")

    print(f"\n[LinkedIn Email DEBUG] --- Step 4: fetching job details for {len(unseen_ids)} job(s) ---")
    jobs = []
    for i, job_id in enumerate(unseen_ids):
        details = _fetch_job_details(job_id)
        jobs.append(details)
        if i < len(unseen_ids) - 1:
            time.sleep(1.5)

    print(f"\n[LinkedIn Email DEBUG] --- Step 5: location filter preview ---")
    from location import passes_location_filter, location_penalty
    passed = []
    for job in jobs:
        passes = passes_location_filter(job)
        penalty = location_penalty(job) if passes else 0
        print(
            f"[LinkedIn Email DEBUG]   '{job.get('title','?')}' @ '{job.get('company','?')}'"
            f"  location='{job.get('location','')}' work_model='{job.get('work_model','')}'"
            f"  passes_location={passes}  penalty={penalty}"
        )
        if passes:
            passed.append(job)
    print(f"[LinkedIn Email DEBUG]   {len(jobs)} fetched → {len(passed)} pass location filter")

    print(f"\n[LinkedIn Email DEBUG] --- Summary ---")
    print(f"[LinkedIn Email DEBUG]   Emails found:           see Step 1 above")
    print(f"[LinkedIn Email DEBUG]   Unique job IDs:         {len(job_ids)}")
    print(f"[LinkedIn Email DEBUG]   After seen_jobs filter: {len(unseen_ids)}")
    print(f"[LinkedIn Email DEBUG]   After location filter:  {len(passed)}")
    print(f"[LinkedIn Email DEBUG]   Returning to pipeline:  {len(jobs)} (location filter applied in main.py, not here)")
    print("[LinkedIn Email DEBUG] ========== fetch_all() END ==========\n")

    print(f"[LinkedIn Email] Extracted {len(jobs)} unique jobs")
    return jobs


def _collect_job_ids_from_email():
    """Connect via IMAP, find LinkedIn alert emails, return deduplicated job IDs."""
    job_ids = []
    try:
        mail = imaplib.IMAP4_SSL(GMAIL_IMAP_SERVER, GMAIL_IMAP_PORT)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select(LINKEDIN_ALERT_FOLDER, readonly=True)

        import state as _state
        MIN_LOOKBACK_DAYS = 7
        last_run = _state.last_run_date()
        floor_date = datetime.now() - timedelta(days=MIN_LOOKBACK_DAYS)
        if last_run and last_run < floor_date:
            since_date = last_run.strftime("%d-%b-%Y")
            print(f"[LinkedIn Email DEBUG]   Lookback: since last run ({since_date})")
        elif last_run:
            since_date = floor_date.strftime("%d-%b-%Y")
            print(f"[LinkedIn Email DEBUG]   Lookback: last run was recent, using {MIN_LOOKBACK_DAYS}-day floor ({since_date})")
        else:
            since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            print(f"[LinkedIn Email DEBUG]   Lookback: first run, using 30-day fallback ({since_date})")
        status, message_ids = mail.search(None, f'ALL SINCE {since_date}')

        if status != "OK" or not message_ids[0]:
            print("[LinkedIn Email] No alert emails found")
            mail.logout()
            return []

        ids = message_ids[0].split()
        print(f"\n[LinkedIn Email DEBUG] --- Step 1: email scan ---")
        print(f"[LinkedIn Email DEBUG]   Label: {LINKEDIN_ALERT_FOLDER}")
        print(f"[LinkedIn Email DEBUG]   Sender filter: none (all senders in label)")
        print(f"[LinkedIn Email DEBUG]   Read filter: none (ALL, not UNSEEN)")
        print(f"[LinkedIn Email DEBUG]   Date filter: SINCE {since_date}")
        print(f"[LinkedIn Email DEBUG]   Emails matching filter: {len(ids)}")

        seen_ids = set()
        for msg_id in ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            body = _get_email_body(msg)
            found_in_email = re.findall(r'linkedin\.com(?:/comm)?/jobs/view/(\d+)', body)
            new_in_email = [jid for jid in found_in_email if jid not in seen_ids]
            print(f"[LinkedIn Email DEBUG]   msg {msg_id.decode()}: {len(found_in_email)} job ID(s) in body, {len(new_in_email)} new → IDs: {new_in_email[:10]}{'...' if len(new_in_email) > 10 else ''}")
            for jid in found_in_email:
                if jid not in seen_ids:
                    seen_ids.add(jid)
                    job_ids.append(jid)

        mail.logout()
        print(f"[LinkedIn Email] Found {len(job_ids)} unique job IDs in emails")
    except imaplib.IMAP4.error as e:
        print(f"[LinkedIn Email] IMAP authentication error: {e}")
    except Exception as e:
        print(f"[LinkedIn Email] Error reading emails: {e}")
    return job_ids


def _fetch_job_details(job_id):
    """Scrape title, company, location, and description from a LinkedIn job page."""
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    base = {
        "title": "",
        "company": "",
        "location": "",
        "work_model": "",
        "comp": "",
        "apply_link": url,
        "description": "",
        # No reliable posted date: LinkedIn's "posted X ago" resets on repost (DEL-05).
        "date_posted": "",
        "posted_precision": "",
        "source": "linkedin_email",
    }
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title_el = (
            soup.select_one("h1.top-card-layout__title") or
            soup.select_one("h1[class*='title']") or
            soup.select_one("h1")
        )
        company_el = (
            soup.select_one("a.topcard__org-name-link") or
            soup.select_one("[class*='company-name']") or
            soup.select_one("a[data-tracking-control-name*='company']")
        )
        location_el = (
            soup.select_one("span.topcard__flavor--bullet") or
            soup.select_one("[class*='job-details-jobs-unified-top-card__bullet']") or
            soup.select_one("[class*='location']")
        )
        desc_el = (
            soup.select_one("div.description__text") or
            soup.select_one("[class*='description']")
        )

        title = title_el.get_text(strip=True) if title_el else ""
        company = company_el.get_text(strip=True) if company_el else ""
        location = location_el.get_text(strip=True) if location_el else ""
        # SRC-21: the scorer's JD_CHARS is the only limit on JD text.
        description = desc_el.get_text(strip=True)[:JD_CHARS] if desc_el else ""

        text = (title + " " + description + " " + location).lower()
        if "remote" in text:
            work_model = "Remote"
        elif "hybrid" in text:
            work_model = "Hybrid"
        else:
            work_model = "Unknown"

        base.update({
            "title": title,
            "company": company,
            "location": location,
            "work_model": work_model,
            "description": description,
        })
        print(f"[LinkedIn Email] {job_id}: '{title}' at '{company}'")
    except Exception as e:
        print(f"[LinkedIn Email] Could not fetch details for job {job_id}: {e}")
    return base


def _get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body
