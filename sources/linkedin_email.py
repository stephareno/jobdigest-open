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


# REV-07: the outcome of each job page fetch, counted per run and shown in
# main.py's fetch summary (and kept for doctor in fetch_budget.json).
OUTCOMES = ("fetched", "blocked", "login_wall", "empty", "error", "kept", "unavailable")
# REV-08: with sources.linkedin_email.fetch_pages false, the counts are these.
ALERT_ONLY_OUTCOMES = ("title_only", "no_alert_details")
# Title, company and location per job id, read from the alert emails
# themselves (REV-07): the fallback when the job page does not load.
ALERT_INFO = {}
# Final-URL markers of LinkedIn's sign-in pages.
LOGIN_WALL_MARKERS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


class LinkedInJobs(list):
    """The run's jobs, plus commit(): save page-failure counts and the run's
    counts (REV-07). main.py calls commit() only after the queue is written,
    so a failure is never counted for a run whose postings were lost."""

    def __init__(self, jobs=(), commit=None, counts=None):
        super().__init__(jobs)
        self._commit = commit
        self.counts = counts or {}
        self.summary = summary_line(self.counts) if counts else ""

    def commit(self):
        if self._commit is not None:
            self._commit()


def summary_line(counts):
    if "title_only" in counts:
        return (f"alert emails only (sources.linkedin_email.fetch_pages false): {counts['title_only']} title-only "
                f"postings, {counts.get('no_alert_details', 0)} jobs skipped (the alert gave no title)")
    return ("job pages: " + ", ".join(f"{name.replace('_', ' ')} {counts.get(name, 0)}" for name in OUTCOMES)
            + f" (of {sum(counts.get(n, 0) for n in ('fetched', 'blocked', 'login_wall', 'empty', 'error'))} "
              f"pages tried)")


def fetch_all():
    print("\n[LinkedIn Email DEBUG] ========== fetch_all() START ==========")
    import config
    import state as _state
    ALERT_INFO.clear()
    job_ids = _collect_job_ids_from_email()

    print(f"\n[LinkedIn Email DEBUG] --- Step 2: URL construction ---")
    if not job_ids:
        print("[LinkedIn Email DEBUG] No job IDs found — returning empty list")
        return []

    urls = [f"https://www.linkedin.com/jobs/view/{jid}/" for jid in job_ids]
    for jid, url in zip(job_ids, urls):
        print(f"[LinkedIn Email DEBUG]   job_id={jid}  url={url}")

    print(f"\n[LinkedIn Email DEBUG] --- Step 3: seen_jobs filter ---")
    unseen_ids = []
    for jid, url in zip(job_ids, urls):
        normalized = _state.normalize_url(url)
        seen = _state.is_seen(url)
        print(f"[LinkedIn Email DEBUG]   {jid}  normalized={normalized}  already_seen={seen}")
        if not seen:
            unseen_ids.append(jid)
    print(f"[LinkedIn Email DEBUG]   {len(job_ids)} total → {len(unseen_ids)} after seen_jobs filter")

    if not fetch_pages():
        return _alert_only_jobs(unseen_ids)

    print(f"\n[LinkedIn Email DEBUG] --- Step 4: fetching job details for {len(unseen_ids)} job(s) ---")
    # REV-07: a page that did not load drops its job, unseen, for a later run
    # to retry; after LINKEDIN_MAX_PAGE_FAILURES runs in a row it is recorded
    # at score 0 ("LinkedIn page unavailable") so it stops costing fetches.
    failures = _state.load_linkedin_failures()
    new_counts, cleared = {}, []
    counts = {name: 0 for name in OUTCOMES}
    jobs = []
    for i, job_id in enumerate(unseen_ids):
        job, outcome = _fetch_page(job_id)
        key = _state.linkedin_fail_key(job.get("apply_link") or f"https://www.linkedin.com/jobs/view/{job_id}/")
        if outcome == "ok":
            counts["fetched"] += 1
            jobs.append(job)
            if key in failures:
                cleared.append(key)
        else:
            counts[outcome] += 1
            failed_runs = failures.get(key, 0) + 1
            alert = ALERT_INFO.get(job_id, {})
            for field in ("title", "company", "location"):
                job[field] = job[field] or alert.get(field, "")
            if failed_runs >= config.LINKEDIN_MAX_PAGE_FAILURES:
                job["unavailable_reason"] = config.LINKEDIN_UNAVAILABLE_REASON
                job["description"] = ""
                counts["unavailable"] += 1
                cleared.append(key)
                jobs.append(job)
                print(f"[LinkedIn Email] {job_id}: page failed {failed_runs} runs in a row ({outcome}); "
                      f"recorded at score 0 as '{config.LINKEDIN_UNAVAILABLE_REASON}'")
            elif config.LINKEDIN_KEEP_ALERT_ONLY_ROWS and all(alert.get(f) for f in ("title", "company", "location")):
                job["description"] = ""
                counts["kept"] += 1
                cleared.append(key)
                jobs.append(job)
                print(f"[LinkedIn Email] {job_id}: page {outcome}; kept from the alert email alone "
                      f"(LINKEDIN_KEEP_ALERT_ONLY_ROWS)")
            else:
                new_counts[key] = failed_runs
                print(f"[LinkedIn Email] {job_id}: page {outcome}; dropped unseen for a later run "
                      f"(failure {failed_runs} of {config.LINKEDIN_MAX_PAGE_FAILURES})")
        if i < len(unseen_ids) - 1:
            time.sleep(1.5)
    print(f"[LinkedIn Email] {summary_line(counts)}")

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

    def commit():
        _state.save_linkedin_failures(new_counts, cleared)
        import fetch_budget
        fetch_budget.record_linkedin(counts)
    return LinkedInJobs(jobs, commit, counts)


def fetch_pages():
    """REV-08: sources.linkedin_email.fetch_pages, the explicit opt-in to
    opening each job's public LinkedIn page (off by default)."""
    return bool((profile_loader.active()["sources"].get("linkedin_email") or {}).get("fetch_pages"))


def alert_only_job(job_id, info):
    """A posting built from the alert email alone (REV-08): no page fetched,
    no description, marked title_only for the scorer, the cap and the card."""
    return {
        "title": info.get("title", ""),
        "company": info.get("company", ""),
        "location": info.get("location", ""),
        "work_model": "Remote" if "remote" in (info.get("title", "") + " " + info.get("location", "")).lower()
                      else "Hybrid" if "hybrid" in info.get("location", "").lower() else "Unknown",
        "comp": "",
        "apply_link": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "description": "",
        "date_posted": info.get("date_posted", ""),
        "posted_precision": "day" if info.get("date_posted") else "",
        "source": "linkedin_email",
        "title_only": True,
    }


def _alert_only_jobs(job_ids):
    """REV-08: fetch_pages false. Every unseen job the alert email names with a
    title becomes a title-only posting; linkedin.com is never requested."""
    counts = {"title_only": 0, "no_alert_details": 0}
    jobs = []
    for job_id in job_ids:
        info = ALERT_INFO.get(job_id) or {}
        if not info.get("title"):
            counts["no_alert_details"] += 1
            print(f"[LinkedIn Email] {job_id}: the alert email gave no title; skipped (no page is fetched)")
            continue
        jobs.append(alert_only_job(job_id, info))
        counts["title_only"] += 1
    print(f"[LinkedIn Email] {summary_line(counts)}")
    print(f"[LinkedIn Email] Extracted {len(jobs)} unique jobs")
    return LinkedInJobs(jobs, None, counts)


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
            for jid, info in alert_details(body).items():
                ALERT_INFO.setdefault(jid, info)
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


def alert_details(body):
    """{job id: {title, company, location}} from one alert email's HTML (REV-07).

    LinkedIn's alert cards link the job title to /jobs/view/<id>, then give
    the company and the location as the next lines ("Acme · Chicago, IL" or
    two separate lines). Best effort: a field it cannot find is left out."""
    if "<" not in body:
        return {}
    soup = BeautifulSoup(body, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"linkedin\.com(?:/comm)?/jobs/view/(\d+)", a["href"])
        title = " ".join(a.get_text(" ", strip=True).split())
        if not m or not title or title.lower() in ("view job", "apply", "see all jobs"):
            continue
        jid = m.group(1)
        if jid in out and out[jid].get("company"):
            continue
        block = a
        for _ in range(4):                         # the card around the link
            if block.parent is None:
                break
            block = block.parent
            lines = [" ".join(t.split()) for t in block.stripped_strings]
            if len(lines) >= 3 and title in lines:
                break
        lines = [" ".join(t.split()) for t in block.stripped_strings]
        after = lines[lines.index(title) + 1:] if title in lines else []
        info = {"title": title}
        if after:
            parts = [p.strip() for p in after[0].split("·")]
            if len(parts) >= 2:
                info["company"], info["location"] = parts[0], parts[1]
            else:
                info["company"] = parts[0]
                if len(after) > 1:
                    info["location"] = after[1]
        out[jid] = info
    return out


def _page_outcome(resp, url):
    """"ok", "blocked" (non-200, LinkedIn's 999 included) or "login_wall"
    (redirected to a sign-in page) for one response."""
    status = getattr(resp, "status_code", 200)
    final = str(getattr(resp, "url", url) or url)
    history = [str(getattr(h, "url", "")) for h in getattr(resp, "history", None) or ()]
    if any(marker in u for u in [final] + history for marker in LOGIN_WALL_MARKERS):
        return "login_wall"
    if status != 200:
        return "blocked"
    return "ok"


def _fetch_page(job_id):
    """(job, outcome) for one LinkedIn job page (REV-07): outcome "ok",
    "blocked" (non-200, 999 included), "login_wall", "error" (the request
    raised) or "empty" (no title or no description parsed)."""
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
    except Exception as e:
        print(f"[LinkedIn Email] Could not fetch details for job {job_id}: {type(e).__name__}: {e}")
        return base, "error"
    outcome = _page_outcome(resp, url)
    if outcome != "ok":
        print(f"[LinkedIn Email] {job_id}: {outcome} (HTTP {getattr(resp, 'status_code', '?')}, "
              f"{getattr(resp, 'url', url)})")
        return base, outcome
    try:
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
    except Exception as e:
        print(f"[LinkedIn Email] Could not parse job {job_id}: {type(e).__name__}: {e}")
        return base, "error"

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
    if not title or not description:
        print(f"[LinkedIn Email] {job_id}: page loaded but parsed no "
              f"{'title' if not title else 'description'}")
        return base, "empty"
    print(f"[LinkedIn Email] {job_id}: '{title}' at '{company}'")
    return base, "ok"


def _fetch_job_details(job_id):
    """The job dict alone, whatever the outcome (kept for callers and tests
    that read the parse; fetch_all uses _fetch_page)."""
    return _fetch_page(job_id)[0]


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
