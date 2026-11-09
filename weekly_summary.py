# weekly_summary.py — the Saturday summary of the week's job search (DEL-12)
#
# Every number and list is computed here from the Google Sheet main index:
# new postings and their score split, applications, rejections and interview
# activity dated in the week, the pipeline to date, declines and rows that
# need fixing by hand. Claude writes one short paragraph, and only when a
# Declined row in the week carries Notes: a reading of the recurring reasons.
# It produces no counts and no scoring advice.
#
# Rejected (the company passed) and Declined (the candidate passed) are separate
# statuses and are never merged in a count or a list.
#
#   python weekly_summary.py                    # build, email, reset the skip counter
#   python weekly_summary.py --dry-run          # print the email body; send and reset nothing
#   python weekly_summary.py --dry-run --as-of 2026-10-10   # as if run that day
#   python weekly_summary.py --scheduled        # the workflow's schedule: run (PUB-08 slot guard)

import argparse
import html
import os
import smtplib
import sys
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
import profile_loader
import schedule
import state
from prompt_template import render_file
from sheets import _get_sheet
from sheet_dates import parse_sheet_date

# How far back the new-postings count reaches, in days, from the run date.
#
# The summary covers the week that ends the day before delivery.weekly_summary_day
# (committed profile: Saturday, so the Saturday–Friday week that just ended). It used to run
# on Friday at 12:00 UTC, seven days back, which put the inclusive cutoff on
# the previous Friday. GAP-08 moved the run to Saturday — the one day with no
# digest — so eight days back keeps the cutoff on that same previous Friday and
# the window does not shift with the later run.
#
# The extra day is not slack: rows carry the fetch date, and Friday's 12:17 and
# 20:17 fetches only reach the Sheet in Sunday's digest. A seven-day window from
# Saturday would cut off at the previous Saturday and lose those rows in both
# the run that is too early to see them and the run that is too late.
LOOKBACK_DAYS = 8

# date.weekday() of the week's last day: the day before the weekly summary day
# (PUB-05, profile delivery.weekly_summary_day; Friday for a Saturday summary).
FRIDAY = (profile_loader.WEEKDAYS.index(profile_loader.active()["delivery"]["weekly_summary_day"]) - 1) % 7

STATUS_APPLIED = config.STATUS_APPLIED
STATUS_INTERVIEWING = config.STATUS_INTERVIEWING
STATUS_REJECTED = "Rejected"  # company passed; never a Declined
STATUS_DECLINED = config.STATUS_DECLINED  # the candidate passed; never a Rejected
STATUS_VALUES = {config.STATUS_NEW, config.STATUS_INTERESTED, STATUS_APPLIED,
                 STATUS_INTERVIEWING, STATUS_REJECTED, STATUS_DECLINED,
                 config.STATUS_CLOSED, config.STATUS_NO_ACTION}

INDEX_DOC_SOURCE = "applied_doc"   # the source key; the Sheet shows "Index Doc"
LATEST_REJECTED_SHOWN = 3
# PUB-04: scoring.summary_model when the profile sets one, else scoring.model.
READING_MODEL = (profile_loader.active()["scoring"].get("summary_model")
                 or profile_loader.active()["scoring"]["model"])


# --- the week ----------------------------------------------------------------

def week_bounds(run_date):
    """(Saturday, Friday) of the week the run reports: the last full Saturday–
    Friday week before run_date. A Saturday run reports the week ending yesterday."""
    back = (run_date.weekday() - FRIDAY) % 7 or 7
    end = run_date - timedelta(days=back)
    return end - timedelta(days=6), end


def postings_cutoff(run_date):
    """First Date First Seen counted as a new posting. On the scheduled Saturday
    run this is run_date - LOOKBACK_DAYS, the Friday before the week (GAP-08)."""
    _, end = week_bounds(run_date)
    return end + timedelta(days=1) - timedelta(days=LOOKBACK_DAYS)


def week_label(start, end):
    """"Week of Oct 3 to Oct 9, 2026"; both years when the week spans two."""
    left = f"{start:%b} {start.day}" + (f", {start.year}" if start.year != end.year else "")
    return f"Week of {left} to {end:%b} {end.day}, {end.year}"


# --- row fields --------------------------------------------------------------

def _text(row, name):
    return str(row.get(name, "") or "").strip()


def score_of(row):
    """The Score cell as an int, or None when blank or not a number."""
    value = row.get("Score", "")
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def event_date(row):
    """When a status-tracked row belongs to a week: Status Date; if blank,
    Applied Date; if both blank, Date First Seen. The last fallback is what
    dates the Applied rows written before Status Date existed (DEL-12)."""
    for name in ("Status Date", "Applied Date", "Date First Seen"):
        d = parse_sheet_date(row.get(name))
        if d is not None:
            return d
    return None


def status_date_of(row):
    """Status Date only. Rejections and interview activity are dated by it alone."""
    return parse_sheet_date(row.get("Status Date"))


def needs_fixing(row):
    """True for an Index Doc row whose heading did not parse into Company and
    Title (the TRK-10 rows). One test, kept here:
      - Title blank,
      - Title equal to a Status value (the heading's tail landed in Title),
      - Company containing " - " or starting with "-" (the split missed),
      - square brackets unbalanced in Company or Title (a "[a | b]" heading
        split down the middle).
    Rows from other sources are never flagged; their Company and Title come
    from the posting, not from a heading parse."""
    if config.source_key(_text(row, "Source")) != INDEX_DOC_SOURCE:
        return False
    company, title = _text(row, "Company"), _text(row, "Job Title")
    if not title or title.lower() in {s.lower() for s in STATUS_VALUES}:
        return True
    if " - " in company or company.startswith("-"):
        return True
    return any(text.count("[") != text.count("]") for text in (company, title))


def _who(row):
    return f"{_text(row, 'Company')}, {_text(row, 'Job Title')}"


def _in(d, start, end):
    return d is not None and start <= d <= end


# --- the summary -------------------------------------------------------------

def build_summary(rows, run_date, skip_stats=(0, 0), read_declines=None, alert_emails=0):
    """The email body as plain text. rows: main-index records (get_all_records).
    skip_stats: (skipped, run_days) from state.get_skip_stats. alert_emails:
    alert-only emails sent for failed fetch runs (REV-02). read_declines:
    called with [{company, title, notes}] only when a Declined row in the week
    has Notes; returns the paragraph. Nothing else here calls Claude."""
    start, end = week_bounds(run_date)
    cutoff = postings_cutoff(run_date)
    out = [week_label(start, end), ""]

    # This week: new postings by Date First Seen over the GAP-08 window.
    new = [r for r in rows if _in(parse_sheet_date(r.get("Date First Seen")), cutoff, end)]
    scores = [score_of(r) for r in new]
    skipped, run_days = skip_stats
    out.append("This week")
    line = f"{len(new)} new postings logged."
    if run_days:
        line += f" {skipped} of {run_days} run days quiet."
        # REV-02: quiet days include alert-only ones (fetch runs failed, no
        # postings went out); say how many.
        line += (f" {alert_emails} alert email{'s' if alert_emails != 1 else ''} sent for failed "
                 f"fetch runs.")
    out.append(line)
    out.append(
        f"Score 7+: {sum(1 for s in scores if s is not None and s >= 7)}"
        f" | 5 to 6: {sum(1 for s in scores if s is not None and 5 <= s <= 6)}"
        f" | 1 to 4: {sum(1 for s in scores if s is not None and s <= 4)}"
        f" | unscored: {sum(1 for s in scores if s is None)}")
    out.append("")

    # Applied this week, dated Status Date -> Applied Date -> Date First Seen.
    applied = [r for r in rows if _text(r, "Status") == STATUS_APPLIED and _in(event_date(r), start, end)]
    applied.sort(key=lambda r: (score_of(r) is None, -(score_of(r) or 0), _who(r).lower()))
    out.append(f"Applied this week: {len(applied)}")
    for r in applied:
        s = score_of(r)
        out.append(f"  {_who(r)} ({s if s is not None else 'no score'})")
    if any(score_of(r) is None for r in applied):
        out.append('  "No score" = added from the Index Doc, no digest row matched.')
    out.append("")

    # Rejections and interview activity: Status Date only.
    for label, status in (("Rejections this week", STATUS_REJECTED),
                          ("Interview activity this week", STATUS_INTERVIEWING)):
        hits = [r for r in rows if _text(r, "Status") == status and _in(status_date_of(r), start, end)]
        if hits:
            out.append(f"{label}: {len(hits)}")
            out.extend(f"  {_who(r)}" for r in hits)
        else:
            out.append(f"{label}: none recorded.")
        out.append("")

    # Pipeline to date.
    interviewing = [r for r in rows if _text(r, "Status") == STATUS_INTERVIEWING]
    rejected = [(i, r) for i, r in enumerate(rows) if _text(r, "Status") == STATUS_REJECTED]
    rejected.sort(key=lambda ir: (event_date(ir[1]) or date.min, ir[0]), reverse=True)
    out.append("Pipeline to date")
    out.append(f"  Interviewing: {len(interviewing)}")
    out.extend(f"    {_who(r)}" for r in interviewing)
    line = f"  Rejected to date: {len(rejected)}"
    if rejected:
        latest = ", ".join(_text(r, "Company") for _, r in rejected[:LATEST_REJECTED_SHOWN])
        line += f" (latest: {latest})"
    out.append(line)
    out.append("")

    # Declined by the candidate this week. Same date fallback as Applied: Declined
    # is set by hand, so most rows have no Status Date.
    declined = [r for r in rows if _text(r, "Status") == STATUS_DECLINED and _in(event_date(r), start, end)]
    noted = [r for r in declined if _text(r, "Notes")]
    out.append(f"Declined by you this week: {len(declined)}")
    if noted:
        out.extend(f"  {_who(r)}: {_text(r, 'Notes')}" for r in noted)
        if read_declines is not None:
            paragraph = read_declines([{"company": _text(r, "Company"), "title": _text(r, "Job Title"),
                                        "notes": _text(r, "Notes")} for r in noted])
            out.extend(["", paragraph])
    out.append("")

    # Needs a look: malformed Index Doc rows (to date) and unscored Applied rows (this week).
    broken = [(i + 2, r) for i, r in enumerate(rows) if needs_fixing(r)]
    unscored = sum(1 for r in applied if score_of(r) is None)
    if broken or unscored:
        out.append("Needs a look")
        if broken:
            out.append(f"  {len(broken)} Index Doc row{'s' if len(broken) != 1 else ''} did not parse "
                       "and need fixing by hand:")
            out.extend(f'    Row {n}: "{_text(r, "Company")} | {_text(r, "Job Title")}"' for n, r in broken)
        if unscored:
            out.append(f"  {unscored} Applied row{'s' if unscored != 1 else ''} this week "
                       f"{'have' if unscored != 1 else 'has'} no score.")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def declines_prompt(profile, listing):
    """PUB-02: prompts/weekly_declines.txt rendered from the profile, with the
    week's Declined notes in place of {{listing}}."""
    return render_file("weekly_declines.txt", profile, listing=listing).rstrip("\n")


def read_declines_with_claude(declines):
    """One short paragraph on recurring reasons in this week's Declined notes."""
    import llm
    listing = "\n".join(f"- {d['title']} at {d['company']}: {d['notes']}" for d in declines)
    prompt = declines_prompt(profile_loader.active(), listing)
    text, _ = llm.complete(READING_MODEL, None, [{"role": "user", "content": prompt}], 300,
                           purpose="weekly summary call")
    print(f"[WeeklySummary] Claude read {len(declines)} Declined note(s)")
    return text.strip()


# --- delivery ----------------------------------------------------------------

def _send_summary(subject, heading, summary_text):
    # An unset GitHub Actions secret renders as "", which .get() would return
    # as-is — so treat empty/whitespace as missing and fall back to config.
    recipient = (os.environ.get("RECIPIENT_EMAIL", "").strip()
                 or profile_loader.active()["delivery"]["email_to"])
    gmail_address = os.environ["GMAIL_ADDRESS"].strip()
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"].strip()
    html_doc = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">
    <div style="margin-bottom:24px;">
      <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;">Job Digest Agent — Weekly Summary</div>
      <div style="font-size:22px;font-weight:700;color:#1a1a1a;">{html.escape(heading)}</div>
    </div>
    <div style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;padding:20px 24px;white-space:pre-wrap;font-size:14px;color:#333;line-height:1.6;">{html.escape(summary_text)}</div>
    <div style="margin-top:24px;font-size:11px;color:#aaa;text-align:center;">
      Job Digest Agent · weekly summary · computed from your Google Sheet
    </div>
  </div>
</body>
</html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient
    msg.attach(MIMEText(html_doc, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, recipient, msg.as_string())
    print(f"[WeeklySummary] Sent: {subject}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Weekly job search summary (DEL-12)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the email body; send nothing, reset nothing")
    parser.add_argument("--as-of", help="run date YYYY-MM-DD (default: today in delivery.timezone)")
    parser.add_argument("--scheduled", action="store_true",
                        help="started by the schedule: trigger; exit 0 unless this is the intended slot (PUB-08)")
    args = parser.parse_args(argv)

    # PUB-08: every cron line of a DST zone fires; the DST twin exits here,
    # before the Sheet is read or the skip counter touched.
    if args.scheduled and not schedule.slot_guard(
            "weekly", profile_loader.active()["delivery"], config.CRON_LEAD_MINUTES,
            config.SLOT_GUARD_MINUTES, cron=os.environ.get("SCHEDULED_CRON") or None,
            off_the_hour=config.CRON_OFF_THE_HOUR_MINUTES):
        return

    print("=== Weekly Summary starting ===")
    run_date = (datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of
                else profile_loader.local_today())
    start, end = week_bounds(run_date)
    rows = _get_sheet().get_all_records()
    print(f"[WeeklySummary] {len(rows)} main-index rows; week {start} to {end}; "
          f"new postings from {postings_cutoff(run_date)}")
    skip_stats = state.get_skip_stats()
    summary = build_summary(rows, run_date, skip_stats, read_declines=read_declines_with_claude,
                            alert_emails=state.get_alert_email_count())
    heading = week_label(start, end)
    subject = f"Weekly job search summary — {heading}"

    if args.dry_run:
        print(f"--- DRY RUN: not sent, skip counter not reset ---\nSubject: {subject}\n")
        print(summary)
        return
    _send_summary(subject, heading, summary)
    state.reset_skip_stats()
    print("Skipped-day counter reset")
    print("=== Done ===")


if __name__ == "__main__":
    main(sys.argv[1:])
