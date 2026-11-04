# sheet_dates.py — parse the date values the Google Sheet hands back
#
# The Sheet's "Date First Seen" column holds a mix: rows written with
# value_input_option=RAW are ISO text ("2026-09-01"), older rows were
# converted by Sheets into real dates, which get_all_records() returns in the
# cell's display format ("9/1/2026") or as a serial number. Comparing those as
# strings against an ISO cutoff made every "9/1/2026" sort after
# "2026-08-28", so the weekly summary counted the whole Sheet. Parse first,
# compare dates. From TRK-08 the writers store serials with a date format, and
# tools/reformat_date_cells.py converted the old text cells; the parser still
# reads all three forms.

from datetime import date, datetime, timedelta

_SHEETS_EPOCH = date(1899, 12, 30)  # Sheets/Excel serial day 0
_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y")


def parse_sheet_date(value):
    """Return a date for any Sheets date representation, or None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Plausible serials only (1982–2119); anything else is not a date.
        return _SHEETS_EPOCH + timedelta(days=int(value)) if 30000 <= value <= 80000 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_sheet_date(float(text))  # a serial number rendered as text
    except ValueError:
        return _parse_text(text)


def to_sheet_serial(value):
    """The Sheets serial day number for a date in any form parse_sheet_date reads,
    or None. Written with value_input_option=RAW, a serial is stored as a number,
    so a date number format on the cell shows it as a real, sortable date (TRK-08)."""
    d = parse_sheet_date(value)
    return (d - _SHEETS_EPOCH).days if d else None


def _parse_text(text):
    head = text.split("T")[0].split(" ")[0] if text[:4].isdigit() else text
    for fmt in _FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


# --- Posted dates (DEL-05, PRD §7) ---------------------------------------------
#
# A job's "date_posted" is the employer's first-posted moment, normalized so
# every source looks alike:
#   "2026-09-15T14:44:49Z"  posted_precision "time"  (the source gave a time)
#   "2026-09-15"            posted_precision "date"  (the source gave a date only)
#   ""                      posted_precision ""      (no reliable date)
# The digest shows hours for "time" and days for "date"; the Sheet's
# "Posted Date" column takes the YYYY-MM-DD part of either.

import re
from datetime import timezone

import profile_loader

PRECISION_TIME = "time"
PRECISION_DATE = "date"

# "Today" and "yesterday" for date-only postings are the candidate's days, not
# UTC's: the profile's delivery.timezone (PUB-05).
DIGEST_TZ = profile_loader.local_tz()

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc_text(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_posted(value, date_only=False):
    """(text, precision) for a raw posted value from any source.

    Accepts ISO dates and datetimes (with or without offset, "Z", fractional
    seconds), "YYYY-MM-DD HH:MM:SS UTC", and epoch seconds or milliseconds.
    A datetime without an offset is read as UTC. date_only=True keeps just the
    source's own calendar date, for sources whose time part is filler (JSearch
    stamps every posting at midnight UTC). Unparseable or empty -> ("", "").
    """
    if value is None or value == "" or isinstance(value, bool):
        return "", ""
    if isinstance(value, (int, float)):
        if value <= 0:
            return "", ""
        seconds = value / 1000 if value > 1e11 else value
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return (dt.strftime("%Y-%m-%d"), PRECISION_DATE) if date_only else (_utc_text(dt), PRECISION_TIME)
    text = str(value).strip()
    if not text:
        return "", ""
    if _DATE_ONLY_RE.match(text):
        try:
            return date.fromisoformat(text).isoformat(), PRECISION_DATE
        except ValueError:
            return "", ""
    if date_only:
        head = text[:10]
        try:
            return date.fromisoformat(head).isoformat(), PRECISION_DATE
        except ValueError:
            return "", ""
    cleaned = re.sub(r"\s*UTC$", "+00:00", text).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return "", ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _utc_text(dt), PRECISION_TIME


_RELATIVE_RE = re.compile(
    r"\b(?P<repost>reposted\s+)?(?:(?P<n>\d+|an?|one)\s+(?P<unit>minute|min|hour|hr|day|week)s?\s+ago"
    r"|(?P<word>just now|today|yesterday))\b",
    re.I,
)


def parse_relative_posted(text, now=None):
    """(text, precision) from relative card text such as "3 Hours Ago" or "Yesterday".

    Minutes and hours give a time; today, yesterday, days and weeks give a date
    (weeks are approximate). "Reposted ..." is the repost date, not the first
    posting, so it gives nothing; neither do months, which are too coarse.
    """
    m = _RELATIVE_RE.search(text or "")
    if not m or m.group("repost"):
        return "", ""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(DIGEST_TZ).date()
    word = (m.group("word") or "").lower()
    if word == "just now":
        return _utc_text(now), PRECISION_TIME
    if word == "today":
        return today.isoformat(), PRECISION_DATE
    if word == "yesterday":
        return (today - timedelta(days=1)).isoformat(), PRECISION_DATE
    raw_n = m.group("n").lower()
    n = 1 if raw_n in ("a", "an", "one") else int(raw_n)
    unit = m.group("unit").lower()
    if unit in ("minute", "min"):
        return _utc_text(now - timedelta(minutes=n)), PRECISION_TIME
    if unit in ("hour", "hr"):
        return _utc_text(now - timedelta(hours=n)), PRECISION_TIME
    days = n * 7 if unit == "week" else n
    return (today - timedelta(days=days)).isoformat(), PRECISION_DATE


def _posted_precision(job):
    precision = job.get("posted_precision") or ""
    if precision:
        return precision
    # Queued before DEL-05, or built by hand: infer from the text's shape.
    text = (job.get("date_posted") or "").strip()
    if not text:
        return ""
    return PRECISION_DATE if _DATE_ONLY_RE.match(text) else PRECISION_TIME


def posted_moment(job):
    """The posting's first-posted moment as an aware UTC datetime, or None.
    A date-only posting counts as midnight at the start of that day in ET."""
    text = (job.get("date_posted") or "").strip()
    precision = _posted_precision(job)
    if not text or not precision:
        return None
    try:
        if precision == PRECISION_DATE:
            d = date.fromisoformat(text[:10])
            return datetime(d.year, d.month, d.day, tzinfo=DIGEST_TZ).astimezone(timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def posted_date_cell(job):
    """The Sheet's "Posted Date": YYYY-MM-DD, or blank when there is no reliable date."""
    moment = posted_moment(job)
    if moment is None:
        return ""
    if _posted_precision(job) == PRECISION_DATE:
        return (job.get("date_posted") or "")[:10]
    return moment.strftime("%Y-%m-%d")


def posted_age_text(job, now=None):
    """ "posted 5h ago" / "posted today" / "posted yesterday" / "posted 3d ago", or "".

    Time sources show hours up to 48h, then days, so an old posting does not
    read as "posted 1400h ago".
    """
    moment = posted_moment(job)
    if moment is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if _posted_precision(job) == PRECISION_TIME:
        hours = max(0, int((now - moment).total_seconds() // 3600))
        if hours < 48:
            return f"posted {hours}h ago" if hours else "posted <1h ago"
        return f"posted {hours // 24}d ago"
    days = (now.astimezone(DIGEST_TZ).date() - date.fromisoformat(job["date_posted"][:10])).days
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    return f"posted {days}d ago"


def is_fresh(job, now=None, window_hours=None, no_flag_vendors=None):
    """True when the role was posted within the fresh window (PRD §7).

    Time sources: within window_hours of now. Date-only sources: today or
    yesterday in ET. Never for a vendor in no_flag_vendors (Oracle HCM, whose
    date can reset on repost), and never without a date.
    """
    if window_hours is None or no_flag_vendors is None:
        from config import FRESH_WINDOW_HOURS, NO_FRESH_FLAG_VENDORS
        window_hours = FRESH_WINDOW_HOURS if window_hours is None else window_hours
        no_flag_vendors = NO_FRESH_FLAG_VENDORS if no_flag_vendors is None else no_flag_vendors
    if (job.get("ats") or "") in no_flag_vendors:
        return False
    moment = posted_moment(job)
    if moment is None:
        return False
    now = now or datetime.now(timezone.utc)
    if _posted_precision(job) == PRECISION_TIME:
        return (now - moment).total_seconds() <= window_hours * 3600
    days = (now.astimezone(DIGEST_TZ).date() - date.fromisoformat(job["date_posted"][:10])).days
    return days <= 1


def newest_first_key(job):
    """Sort key putting newer postings first and undated postings last."""
    moment = posted_moment(job)
    return (0, -moment.timestamp()) if moment else (1, 0)


def freshness_counts(jobs, now=None):
    """(with a posted date, flagged fresh) over a batch of jobs."""
    dated = fresh = 0
    for job in jobs:
        if posted_moment(job) is not None:
            dated += 1
            if is_fresh(job, now=now):
                fresh += 1
    return dated, fresh
