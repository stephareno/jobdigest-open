# sheets.py — writes jobs to Google Sheet index

import os
import json
import time
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import profile_loader
from config import (SHEET_NAME, SHEET_COLUMNS, STATUS_NEW, DISQUALIFIED_SHEET_NAME, SOURCE_DISPLAY_NAMES,
                    source_display, source_key)
from sheet_dates import posted_date_cell, to_sheet_serial
from sheet_layout import DISQUALIFIED_COLUMNS

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]


def _get_spreadsheet():
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def _get_or_create_sheet(spreadsheet, title, headers):
    try:
        sheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
    existing = sheet.row_values(1)
    if not existing or existing[0] != headers[0]:
        sheet.insert_row(headers, 1)
        try:
            sheet.format("1:1", {"textFormat": {"bold": True}})
        except Exception as fmt_err:
            print(f"[Sheets] Warning: could not bold header row for '{title}': {fmt_err}")
    return sheet


def _get_sheet():
    spreadsheet = _get_spreadsheet()
    return _get_or_create_sheet(spreadsheet, SHEET_NAME, SHEET_COLUMNS)


def _ensure_header(sheet, headers, name):
    """Append a missing header to the end of row 1 (never moves columns)."""
    if name not in headers:
        sheet.update_cell(1, len(headers) + 1, name)
        headers.append(name)
        print(f"[Sheets] Added missing column '{name}'")
    return headers


def _col_index(headers, name):
    """Return 1-based column index for a header name. Raises if not found."""
    try:
        return headers.index(name) + 1
    except ValueError:
        raise ValueError(f"[Sheets] Column '{name}' not found in sheet headers: {headers}")


def _get_existing_urls(sheet, headers):
    """Look up the Apply Link column by name, not hardcoded index.

    Raises on a read failure (TRK-06). An empty set here would re-append every
    queued row as a duplicate, so a failed read must fail the write instead."""
    col = _col_index(headers, "Apply Link")
    col_values = sheet.col_values(col)
    return set(v.strip() for v in col_values[1:] if v.strip())


# TRK-08: date columns hold real date cells on both tabs. Every writer uses RAW,
# which keeps an ISO string as text (unsortable), so dates go in as serial day
# numbers and the cells get the M/D/YYYY format the older real dates already show.
# Only these columns are converted; comp, titles and URLs stay verbatim.
DATE_COLUMNS = ("Date First Seen", "Posted Date", "Applied Date", "Status Date")
DATE_FORMAT = {"numberFormat": {"type": "DATE", "pattern": "M/d/yyyy"}}


def date_cell(value):
    """A date column's cell value: a serial number Sheets stores as a date under
    RAW, "" for blank. Text that is not a date is kept as written and logged."""
    if value is None or str(value).strip() == "":
        return ""
    serial = to_sheet_serial(value)
    if serial is None:
        print(f"[Sheets] Warning: {value!r} is not a date; written as text")
        return value
    return serial


def cell_value(header, value):
    """The value to write under a header: date columns via date_cell, the rest verbatim."""
    return date_cell(value) if header in DATE_COLUMNS else value


def _row_values(job_data, headers):
    return [cell_value(h, job_data.get(h, "")) for h in headers]


def format_date_cells(sheet, headers, first_row, last_row):
    """Apply DATE_FORMAT to every date column present, rows first_row..last_row.
    One API call for all columns.

    Uses updateCells with one entry per cell, not repeatCell (gspread's
    batch_format): repeatCell skips rows hidden by a filter, and the main tab's
    filter hides every Score 1-4 row, so 242 of them kept showing raw serials."""
    requests = []
    for name in DATE_COLUMNS:
        if name in headers:
            col = headers.index(name)
            requests.append({"updateCells": {
                "range": {"sheetId": sheet.id, "startRowIndex": first_row - 1, "endRowIndex": last_row,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rows": [{"values": [{"userEnteredFormat": DATE_FORMAT}]}] * (last_row - first_row + 1),
                "fields": "userEnteredFormat.numberFormat",
            }})
    if requests:
        sheet.spreadsheet.batch_update({"requests": requests})
    return len(requests)


def _append_at_end(sheet, rows, headers):
    """Write rows directly below the last row with data. Returns the first row number.

    TRK-06: never uses values.append (gspread append_row/append_rows). Its table
    detection decided in Sep 2026 that the main tab ended at row 1211, so every
    "append" overwrote row 1212 and the run still logged success. This writes to
    an explicit range and raises if the API reports writing anywhere else.
    TRK-08: the new rows' date cells are then given the date format, so they
    never depend on Sheets carrying a column's format onto added rows.
    """
    if not rows:
        return 0
    last = len(sheet.get_all_values())  # the API drops trailing empty rows
    start, end = last + 1, last + len(rows)
    if end > sheet.row_count:
        sheet.add_rows(end - sheet.row_count)
    width = max(len(r) for r in rows)
    target = f"A{start}:{gspread.utils.rowcol_to_a1(end, width)}"
    resp = sheet.update(rows, target, value_input_option="RAW")
    written = (resp or {}).get("updatedRange", "").split("!")[-1]
    if written.split(":")[0] != f"A{start}":
        raise RuntimeError(
            f"[Sheets] '{sheet.title}': asked to write {target}, API reports {written or 'nothing'}")
    format_date_cells(sheet, headers, start, end)
    print(f"[Sheets] '{sheet.title}': wrote rows {start}-{end}")
    return start


def _first_seen(job):
    """The date a posting was found. OPS-04: fetch runs score and stamp found_at;
    the digest writes the Sheet later, so the write time is not the find time."""
    found_at = (job.get("found_at") or "").strip()
    return found_at[:10] if found_at else datetime.utcnow().strftime("%Y-%m-%d")


def main_row_data(job):
    """{header: value} for one main-index row. Shared by append_jobs and the
    TRK-07 backfill, which fills blank cells of Index Doc rows from it."""
    url = job.get("apply_link", "").strip()
    return {
        "Job Title": job.get("title", ""),
        "Company": job.get("company", ""),
        "Location": job.get("location", ""),
        "Work Model": job.get("work_model", ""),
        "Comp": job.get("comp", ""),
        "Score": job.get("score", ""),
        "Claude Rationale": job.get("rationale", ""),
        "Source": source_display(job.get("source", "")),
        "Date First Seen": _first_seen(job),
        "Apply Link": url,
        "Status": STATUS_NEW,
        "Notes": "",
        "Warm Contact": job.get("warm_contact", ""),
        # SCO-09: written only if the candidate adds the column; never created
        "Function Fit": job.get("function_fit", ""),
        # PUB-09: the role criteria the scoring call found; only if the column exists
        "Criteria": ", ".join(job.get("criteria_hits") or ()),
        "Relo": "Y" if job.get("relo") else "",
        # PUB-16: only if the candidate adds the column; never created
        "Dream": "Y" if job.get("is_dream_company") else "",
        "Tier": job.get("tier", "dream" if job.get("is_dream_company") else ""),
        "Posted Date": posted_date_cell(job),
    }


def append_jobs(jobs):
    """Write score 1+ jobs to the main index. Raises on any Sheet failure (TRK-06):
    the digest catches it, still emails and dequeues, then fails the run."""
    if not jobs:
        return 0
    sheet = _get_sheet()
    headers = _ensure_header(sheet, sheet.row_values(1), "Relo")
    headers = _ensure_header(sheet, headers, "Tier")
    headers = _ensure_header(sheet, headers, "Posted Date")
    existing_urls = _get_existing_urls(sheet, headers)
    rows = []
    for job in jobs:
        url = job.get("apply_link", "").strip()
        if not url or url in existing_urls:
            continue
        # Build row keyed by header name — safe even if columns are reordered
        job_data = main_row_data(job)
        rows.append(_row_values(job_data, headers))
        existing_urls.add(url)
    _append_at_end(sheet, rows, headers)
    print(f"[Sheets] Added {len(rows)} new jobs to index")
    return len(rows)


_DISQ_COLUMNS = DISQUALIFIED_COLUMNS


def append_disqualified_jobs(jobs):
    """Write score-0 jobs to the Disqualified tab, not the main index.
    Raises on any Sheet failure, like append_jobs (TRK-06)."""
    if not jobs:
        return 0
    spreadsheet = _get_spreadsheet()
    sheet = _get_or_create_sheet(spreadsheet, DISQUALIFIED_SHEET_NAME, _DISQ_COLUMNS)
    headers = _ensure_header(sheet, sheet.row_values(1), "Relo")
    headers = _ensure_header(sheet, headers, "Tier")
    headers = _ensure_header(sheet, headers, "Posted Date")
    existing_urls = _get_existing_urls(sheet, headers)
    rows = []
    for job in jobs:
        url = job.get("apply_link", "").strip()
        if not url or url in existing_urls:
            continue
        job_data = {
            "Job Title": job.get("title", ""),
            "Company": job.get("company", ""),
            "Location": job.get("location", ""),
            "Work Model": job.get("work_model", ""),
            "Comp": job.get("comp", ""),
            "Score": 0,
            "Disqualify Reason": job.get("rationale", ""),
            "Source": source_display(job.get("source", "")),
            "Date First Seen": _first_seen(job),
            "Apply Link": url,
            "Relo": "Y" if job.get("relo") else "",
            # PUB-16: only if the candidate adds the column; never created
            "Dream": "Y" if job.get("is_dream_company") else "",
            "Tier": job.get("tier", "dream" if job.get("is_dream_company") else ""),
            "Posted Date": posted_date_cell(job),
        }
        rows.append(_row_values(job_data, headers))
        existing_urls.add(url)
    _append_at_end(sheet, rows, headers)
    print(f"[Sheets] Added {len(rows)} disqualified jobs to Disqualified tab")
    return len(rows)


def append_jobs_batched(jobs, batch_size=100, sleep_secs=5.0):
    """Write jobs to the main index in bulk to minimize API calls."""
    if not jobs:
        return 0
    try:
        sheet = _get_sheet()
        headers = _ensure_header(sheet, sheet.row_values(1), "Relo")
        headers = _ensure_header(sheet, headers, "Tier")
        headers = _ensure_header(sheet, headers, "Posted Date")
        existing_urls = _get_existing_urls(sheet, headers)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        rows = []
        for job in jobs:
            url = job.get("apply_link", "").strip()
            if not url or url in existing_urls:
                continue
            job_data = {
                "Job Title": job.get("title", ""),
                "Company": job.get("company", ""),
                "Location": job.get("location", ""),
                "Work Model": job.get("work_model", ""),
                "Comp": job.get("comp", ""),
                "Score": job.get("score", ""),
                "Claude Rationale": job.get("rationale", ""),
                "Source": source_display(job.get("source", "")),
                "Date First Seen": today,
                "Apply Link": url,
                "Status": STATUS_NEW,
                "Notes": "",
                "Warm Contact": job.get("warm_contact", ""),
                # SCO-09: written only if the candidate adds the column; never created
                "Function Fit": job.get("function_fit", ""),
                # PUB-09: written only if the candidate adds the column; never created
                "Criteria": ", ".join(job.get("criteria_hits") or ()),
                "Relo": "Y" if job.get("relo") else "",
                # PUB-16: only if the candidate adds the column; never created
                "Dream": "Y" if job.get("is_dream_company") else "",
                "Tier": job.get("tier", "dream" if job.get("is_dream_company") else ""),
                "Posted Date": posted_date_cell(job),
            }
            rows.append(_row_values(job_data, headers))
            existing_urls.add(url)
        if not rows:
            print("[Sheets] Main index: 0 new rows to write")
            return 0
        # Write in chunks — each chunk is one write request
        added = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            _append_at_end(sheet, chunk, headers)
            added += len(chunk)
            print(f"[Sheets] Main index: {added}/{len(rows)} written")
            if i + batch_size < len(rows):
                time.sleep(sleep_secs)
        print(f"[Sheets] Added {added} new jobs to main index")
        return added
    except Exception as e:
        print(f"[Sheets] Error in append_jobs_batched: {e}")
        return 0


def append_disqualified_jobs_batched(jobs, batch_size=100, sleep_secs=5.0):
    """Write disqualified jobs to the Disqualified tab in bulk."""
    if not jobs:
        return 0
    try:
        spreadsheet = _get_spreadsheet()
        sheet = _get_or_create_sheet(spreadsheet, DISQUALIFIED_SHEET_NAME, _DISQ_COLUMNS)
        headers = _ensure_header(sheet, sheet.row_values(1), "Relo")
        headers = _ensure_header(sheet, headers, "Tier")
        headers = _ensure_header(sheet, headers, "Posted Date")
        existing_urls = _get_existing_urls(sheet, headers)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        rows = []
        for job in jobs:
            url = job.get("apply_link", "").strip()
            if not url or url in existing_urls:
                continue
            job_data = {
                "Job Title": job.get("title", ""),
                "Company": job.get("company", ""),
                "Location": job.get("location", ""),
                "Work Model": job.get("work_model", ""),
                "Comp": job.get("comp", ""),
                "Score": 0,
                "Disqualify Reason": job.get("rationale", ""),
                "Source": source_display(job.get("source", "")),
                "Date First Seen": today,
                "Apply Link": url,
                "Relo": "Y" if job.get("relo") else "",
                # PUB-16: only if the candidate adds the column; never created
                "Dream": "Y" if job.get("is_dream_company") else "",
                "Tier": job.get("tier", "dream" if job.get("is_dream_company") else ""),
                "Posted Date": posted_date_cell(job),
            }
            rows.append(_row_values(job_data, headers))
            existing_urls.add(url)
        if not rows:
            print("[Sheets] Disqualified: 0 new rows to write")
            return 0
        # Write in chunks — each chunk is one write request
        added = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            _append_at_end(sheet, chunk, headers)
            added += len(chunk)
            print(f"[Sheets] Disqualified: {added}/{len(rows)} written")
            if i + batch_size < len(rows):
                time.sleep(sleep_secs)
        print(f"[Sheets] Added {added} disqualified jobs to Disqualified tab")
        return added
    except Exception as e:
        print(f"[Sheets] Error in append_disqualified_jobs_batched: {e}")
        return 0


def delete_rows(sheet, row_numbers):
    """Delete rows by 1-based number, bottom-up so the numbers still to delete
    stay valid. Callers re-read the tab first: numbers go stale on any write."""
    if any(number < 2 for number in row_numbers):
        raise ValueError(f"[Sheets] refusing to delete the header row of '{sheet.title}': {sorted(row_numbers)}")
    for number in sorted(set(row_numbers), reverse=True):
        sheet.delete_rows(number)
    return len(set(row_numbers))


def get_declined_feedback():
    """
    Return a list of {company, title, notes} dicts for Declined jobs that
    have notes. Used to inform the scorer so patterns don't recur.
    """
    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()
        feedback = []
        for r in all_rows:
            if r.get("Status", "") == "Declined" and r.get("Notes", "").strip():
                feedback.append({
                    "company": r.get("Company", ""),
                    "title": r.get("Job Title", ""),
                    "notes": r.get("Notes", "").strip(),
                })
        return feedback
    except Exception as e:
        print(f"[Sheets] Error reading declined feedback: {e}")
        return []


def get_index_rows():
    """Return [{row, company, title, status, applied_date, first_seen}] for every main-index row.
    Row numbers are 1-based sheet positions (header is row 1)."""
    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()
        return [
            {
                "row": i + 2,
                "company": str(r.get("Company", "")),
                "title": str(r.get("Job Title", "")),
                "status": str(r.get("Status", "")),
                "applied_date": str(r.get("Applied Date", "")).strip(),
                "first_seen": str(r.get("Date First Seen", "")).strip(),
            }
            for i, r in enumerate(all_rows)
        ]
    except Exception as e:
        print(f"[Sheets] Error reading index rows: {e}")
        return []


def get_recent_index_rows(days):
    """Main-index rows first seen in the last `days` days, for FIL-13's digest
    collapse: [{title, company, apply_link, source, score, first_seen}],
    first_seen a date. Company carries the employer and its via tail. A failed
    read is a warning, never a blocked digest: the collapse then runs within
    the batch only."""
    from sheet_dates import parse_sheet_date
    try:
        all_rows = _get_sheet().get_all_records()
    except Exception as e:
        print(f"::warning title=Recent Sheet rows unavailable::{type(e).__name__}: {e} — "
              "digest collapses within this batch only (FIL-13)")
        return []
    today = datetime.utcnow().date()
    out = []
    for r in all_rows:
        seen = parse_sheet_date(r.get("Date First Seen", ""))
        if seen is None or not 0 <= (today - seen).days <= days:
            continue
        out.append({
            "title": str(r.get("Job Title", "")).strip(),
            "company": str(r.get("Company", "")).strip(),
            "apply_link": str(r.get("Apply Link", "")).strip(),
            "source": source_key(str(r.get("Source", "")).strip()),
            "score": r.get("Score", ""),
            "first_seen": seen,
        })
    return out


def ensure_columns(names):
    """Append any missing header names to the end of the header row.
    Existing columns are never moved — lookup stays by header name."""
    try:
        sheet = _get_sheet()
        headers = sheet.row_values(1)
        for name in names:
            if name not in headers:
                sheet.update_cell(1, len(headers) + 1, name)
                headers.append(name)
                print(f"[Sheets] Added missing column '{name}'")
        return headers
    except Exception as e:
        print(f"[Sheets] Error ensuring columns: {e}")
        return []


def update_row_fields(row_number, fields):
    """Write {header_name: value} onto one existing row. Never creates rows."""
    try:
        sheet = _get_sheet()
        headers = sheet.row_values(1)
        for name, value in fields.items():
            sheet.update_cell(row_number, _col_index(headers, name), cell_value(name, value))
        if any(name in DATE_COLUMNS for name in fields):
            format_date_cells(sheet, [h if h in fields else "" for h in headers],
                              row_number, row_number)
        return True
    except Exception as e:
        print(f"[Sheets] Error updating row {row_number}: {e}")
        return False


def status_date():
    """DEL-12: the Status Date written beside every Status change: the run's
    date in the profile's delivery.timezone (PUB-05; the weekly summary counts
    the candidate's week in that zone). Rows set before DEL-12 stay blank; the
    weekly summary falls back to Applied Date, then Date First Seen, for those."""
    return profile_loader.local_today().isoformat()


def append_applied_row(company, title, applied_date=""):
    """Append a minimal Applied row (seeded from the index doc).
    Returns the new row number, or 0 on failure."""
    try:
        sheet = _get_sheet()
        headers = _ensure_header(sheet, sheet.row_values(1), "Status Date")
        job_data = {
            "Job Title": title,
            "Company": company,
            "Status": "Applied",
            "Status Date": status_date(),
            "Applied Date": applied_date,
            "Source": SOURCE_DISPLAY_NAMES["applied_doc"],
            "Date First Seen": datetime.utcnow().strftime("%Y-%m-%d"),
        }
        return _append_at_end(sheet, [_row_values(job_data, headers)], headers)
    except Exception as e:
        print(f"[Sheets] Error appending applied row: {e}")
        return 0


def update_statuses(updates):
    """Write Status values onto existing rows. updates: [(row_number, status)].
    Each row also gets Status Date (DEL-12), a real date cell, created at the
    end of the header row if missing.
    Column located by header name per non-negotiable #4. Never creates rows."""
    if not updates:
        return 0
    try:
        sheet = _get_sheet()
        headers = _ensure_header(sheet, sheet.row_values(1), "Status Date")
        col = _col_index(headers, "Status")
        date_col = _col_index(headers, "Status Date")
        today = date_cell(status_date())
        written = 0
        for row, status in updates:
            sheet.update_cell(row, col, status)
            sheet.update_cell(row, date_col, today)
            format_date_cells(sheet, [h if h == "Status Date" else "" for h in headers], row, row)
            written += 1
        return written
    except Exception as e:
        print(f"[Sheets] Error updating statuses: {e}")
        return 0


def get_unseen_jobs():
    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()
        return [
            {
                "title": r.get("Job Title", ""),
                "company": r.get("Company", ""),
                "location": r.get("Location", ""),
                "work_model": r.get("Work Model", ""),
                "comp": r.get("Comp", ""),
                "score": r.get("Score", ""),
                "rationale": r.get("Claude Rationale", ""),
                "source": source_key(r.get("Source", "")),
                "apply_link": r.get("Apply Link", ""),
                "warm_contact": r.get("Warm Contact", ""),
                "status": r.get("Status", ""),
            }
            for r in all_rows
            if r.get("Status", "") in ("New", "No action taken")
        ]
    except Exception as e:
        print(f"[Sheets] Error reading fallback jobs: {e}")
        return []
