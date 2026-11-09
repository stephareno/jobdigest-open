# ============================================================
# sources/status_email.py — Gmail application-status tracking (PRD §15)
# ============================================================
#
# Reads the Gmail inbox (any sender: Greenhouse, Lever, Workday, Ashby,
# corporate domains) for application-lifecycle emails and updates the Status
# column of the matching row in the main index.
#
# TRK-11: each candidate email is read in full by Claude, which returns
#   status      Rejected | Interviewing | Applied | none
#   row         which candidate row the email is about, or none
#   confidence  high | medium | low
#   reason      one sentence
# The phrase lists this replaced (TRK-05) missed rejections worded any other
# way ("decided to proceed with other candidates") and matched phrases inside
# hypothetical clauses. They are frozen in tools/trk05_rules.py for the audit.
#
# Candidate (cheap prefilter, in code, no Claude call): the email names a
# company that has a row in the main index (word-boundary match against
# Company: the employer, or the poster in "<employer> (via <poster>)",
# FIL-13). Mail the candidate sent themself (digests, their own replies) is skipped.
#
# Rows with the same company and title are one job (one posting found on
# several boards). Claude sees one line per job, labeled with the row furthest
# along; a write goes to the job's engaged rows (Interested or later), or to
# its only row. A job on several rows with none engaged is logged, not guessed.
#
# Write rule: only confidence high, only a forward transition (_RANK), and
# Rejected only when the row was matched by company + title (the title appears
# in the email). A row still at New needs the title match for any status: a
# company name can be an ordinary word ("Next Steps" names Step). Everything else is logged as a decision line, and a status
# that is not written is logged UNMATCHED, so the candidate can set the row by
# hand. No row is ever created.
#
# "Rejected" (company passed) is deliberately distinct from "Declined"
# (the candidate passed): the scoring feedback loop reads Declined rows as their
# preferences and must never learn from company rejections. Declined and
# Closed are manual and never overwritten.
#
# Re-processing the same email is a no-op (forward-only), so writes need no
# per-message state (fixed 14-day lookback). Every Status written also writes
# Status Date on the row (DEL-12, in sheets.update_statuses).
#
# TRK-12: an email stays in the lookback for ~14 digest runs, so Claude's
# reply is cached in config.STATUS_EMAIL_CACHE_FILE, keyed by Message-ID and
# checked against a hash of the exact prompt (model, system prompt, candidate
# lines with their current statuses, email). A hit skips only the call: the
# reply still goes through parse_response and decide() against today's rows.
# A changed row, status or prompt is a miss and is classified again. Entries
# not used for LOOKBACK_DAYS are pruned. Only the digest writes the file
# (daily.yml commits it); failed calls are never cached.
#
# Dry run: python -m sources.status_email --dry-run [--days 60]
# replays the inbox against the live Sheet and prints every decision, writing
# nothing. It reads the cache but never writes it.

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

from bs4 import BeautifulSoup

import llm
from llm import ApiAccessError
from config import (GMAIL_IMAP_SERVER, GMAIL_IMAP_PORT, STATUS_EMAIL_MODEL,
                    STATUS_EMAIL_MAX_BODY_CHARS, STATUS_EMAIL_MAX_ROWS,
                    STATUS_EMAIL_DRY_RUN_DAYS, STATUS_EMAIL_THINKING,
                    STATUS_EMAIL_MAX_IN_FLIGHT, STATUS_EMAIL_CACHE_FILE)
import profile_loader
import sheets
from sheet_dates import parse_sheet_date
from state import company_names, write_json_atomic

CACHE_PATH = Path(__file__).resolve().parent.parent / STATUS_EMAIL_CACHE_FILE

LOOKBACK_DAYS = 14

STATUS_RECEIVED = "Applied"
STATUS_INTERVIEW = "Interviewing"
STATUS_REJECTED = "Rejected"
STATUS_NONE = "none"
STATUSES = (STATUS_REJECTED, STATUS_INTERVIEW, STATUS_RECEIVED, STATUS_NONE)
CONFIDENCES = ("high", "medium", "low")
CONFIDENCE_TO_WRITE = "high"

# Forward-only transition ranks. Declined/Closed are manual/terminal and
# never overwritten (absent from the map on purpose).
_RANK = {"": 0, "New": 0, "No action taken": 0, "Interested": 1,
         "Applied": 2, "Interviewing": 3, "Rejected": 4}

MAX_TOKENS = 300

_SYSTEM_PROMPT_TEMPLATE = """You read one email from <<NAME>>'s inbox and decide whether it changes
the status of one of <<POS>> job applications. Return ONLY valid JSON, no prose, no
markdown fences:
{"status": "Rejected" | "Interviewing" | "Applied" | "none",
 "row": <the row number from the candidate list, or null>,
 "confidence": "high" | "medium" | "low",
 "reason": "<one sentence>"}

Statuses:
- Rejected: the employer (or its recruiter) tells <<SHORT>> that <<POS_UP>> OWN
  application or candidacy will not go further: not selected, decided to
  proceed or move forward with other candidates, no longer under
  consideration, the position <<SUBJ>> applied for was filled. A rejection must be
  about <<POS>> own application, said as a decision already made.
- Interviewing: the employer invites <<OBJ>> to an interview, phone screen or
  recruiter call, asks <<OBJ>> to pick or schedule a time, or confirms one.
- Applied: the employer confirms it received <<POS>> application or resume.
- none: everything else. Job alerts and job recommendations (LinkedIn, Indeed,
  Glassdoor and similar), newsletters, marketing, digests, account
  verification or welcome emails, introductions between people, emails <<SUBJ>>
  sent, and any email that only mentions the company are "none".

Hypothetical language is not a decision. An acknowledgment that only
describes what might happen later is "Applied" (or "none" if it does not
confirm an application), never "Interviewing" or "Rejected". Examples:
"if you are selected we will contact you to schedule an interview",
"you may be contacted to schedule an interview if your experience is a match",
"if we don't move forward with your application we'll keep you in mind",
"if you are not selected for this position, keep an eye on our careers page".

Row: pick the candidate row the email is about, by company and job title.
Use null if the email is about none of them, or if you cannot tell which of
several rows it means; never guess between rows. A line marked "[same job
also on rows ...]" is one job listed once: if the email is about that job,
answer with the line's own row number. The candidates were found by
a plain word match, so a company whose name is an ordinary word ("Step",
"Cape", "Confidential") may appear only by accident: the email must be from or
about that company's hiring for that role. An email about a job at a company
that is not in the list is row null, even if its status is clear.

Confidence: "high" only when the email's meaning is unambiguous and the row
is clear. Use "medium" or "low" when either is uncertain.

Reason: one sentence saying what the email says, quoting its key words."""


def status_prompt(profile):
    """The classifier's system prompt for a profile's candidate: the name and
    pronouns come from candidate.* (PUB-05). Rendered byte for byte the same
    for the same profile, so TRK-12's prompt-hash cache stays valid."""
    c = profile["candidate"]
    pr = profile_loader.PRONOUNS[c["pronouns"]]
    text = _SYSTEM_PROMPT_TEMPLATE
    for token, value in (("<<NAME>>", c["display_name"]), ("<<SHORT>>", c["short_name"]),
                         ("<<POS_UP>>", pr["possessive"].upper()), ("<<POS>>", pr["possessive"]),
                         ("<<OBJ>>", pr["object"]), ("<<SUBJ>>", pr["subject"])):
        text = text.replace(token, value)
    return text


SYSTEM_PROMPT = status_prompt(profile_loader.active())


def _normalize(text):
    """Lowercase, collapse everything non-alphanumeric to single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _header(msg, name):
    """Decoded header value, single line."""
    try:
        value = str(make_header(decode_header(msg.get(name, "") or "")))
    except Exception:
        value = msg.get(name, "") or ""
    return " ".join(value.split())


_URL = re.compile(r"https?://([^/\s)>\]]+)[^\s)>\]]*")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def _email_body(msg):
    """Plain-text body: the text/plain part when there is one, otherwise the
    HTML part with tags stripped. Links are shortened to their host (job-alert
    tracking URLs run to hundreds of characters each and carry no meaning)."""
    plain, html = [], []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            chunk = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            continue
        if content_type == "text/plain":
            plain.append(chunk)
        else:
            html.append(BeautifulSoup(chunk, "html.parser").get_text("\n", strip=True))
    body = "\n".join(plain) if "".join(plain).strip() else "\n".join(html)
    body = _URL.sub(lambda m: f"[link: {m.group(1)}]", body)
    return _BLANK_LINES.sub("\n\n", body).strip()


def email_fields(msg):
    """{subject, from, date, body} of one message."""
    return {
        "subject": _header(msg, "Subject"),
        "from": _header(msg, "From"),
        "date": _header(msg, "Date"),
        "message_id": _header(msg, "Message-ID"),
        "body": _email_body(msg),
    }


def email_text(fields):
    """Subject + body, normalized, for the company and title matches."""
    return _normalize(fields["subject"] + " " + fields["body"])


def _rank(row):
    return _RANK.get(row["status"], 99)


def _first_seen_key(row):
    seen = parse_sheet_date(row.get("first_seen", ""))
    return seen.toordinal() if seen else 10**9


def candidate_jobs(text, rows):
    """Jobs from the index whose company appears in normalized email text.

    A row answers to its employer and, when Company carries a via tail, to the
    poster too (FIL-13). Rows with the same company and title are one job
    (the same posting seen on several boards): a job is {"row": representative
    row, "rows": every row of it}. The representative is the row furthest
    along (highest status rank), then the earliest seen, so Claude sees the
    row the candidate applied through. Jobs whose title appears in the email come
    first, then engaged ones; capped at STATUS_EMAIL_MAX_ROWS.
    """
    groups = {}
    for r in rows:
        for name in company_names(r["company"]):
            # "The Johns Hopkins University" also answers to "Johns Hopkins University".
            key = re.sub(r"^the ", "", _normalize(name))
            if key and re.search(r"\b" + re.escape(key) + r"\b", text):
                groups.setdefault((_normalize(r["company"]), _normalize(r["title"])), []).append(r)
                break
    jobs = []
    for members in groups.values():
        rep = min(members, key=lambda r: (-(_rank(r) if _rank(r) != 99 else 5),
                                          _first_seen_key(r), r["row"]))
        jobs.append({"row": rep, "rows": sorted(members, key=lambda r: r["row"])})

    def order(job):
        title = _normalize(job["row"]["title"])
        return (not (title and title in text), _rank(job["row"]) == 0, job["row"]["row"])
    return sorted(jobs, key=order)[:STATUS_EMAIL_MAX_ROWS]


def build_user_message(fields, jobs):
    lines = []
    for job in jobs:
        r = job["row"]
        others = [o["row"] for o in job["rows"] if o is not r]
        dup = f" [same job also on rows {', '.join(map(str, others))}]" if others else ""
        lines.append(f"Row {r['row']}: {r['company']} — {r['title']} "
                     f"(current status: {r['status'] or 'New'}){dup}")
    body = fields["body"]
    if len(body) > STATUS_EMAIL_MAX_BODY_CHARS:
        body = body[:STATUS_EMAIL_MAX_BODY_CHARS] + "\n[... body cut at {:,} characters]".format(
            STATUS_EMAIL_MAX_BODY_CHARS)
    possessive = profile_loader.PRONOUNS[profile_loader.active()["candidate"]["pronouns"]]["possessive"]
    return (f"Candidate rows from {possessive} job index:\n" + "\n".join(lines) + "\n\n"
            f"Email:\nFrom: {fields['from']}\nDate: {fields['date']}\n"
            f"Subject: {fields['subject']}\n\n{body}")


def parse_response(raw, jobs):
    """Validated classification from Claude's reply text.

    Returns {status, job (a candidate job or None), confidence, reason}. A row
    number from any row of a job selects that job. Raises ValueError on a
    reply that is not the JSON asked for.
    """
    text = (raw or "").strip()
    # The reply is asked to be JSON only; take the last {...} object if prose
    # slipped in around it.
    objects = re.findall(r"\{[^{}]*\}", text, flags=re.S)
    if not objects:
        raise ValueError(f"no JSON object in reply: {text[:200]!r}")
    try:
        data = json.loads(objects[-1])
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable JSON in reply ({e}): {text[:200]!r}")
    status = {s.lower(): s for s in STATUSES}.get(str(data.get("status", "")).strip().lower())
    confidence = str(data.get("confidence", "")).strip().lower()
    if status is None or confidence not in CONFIDENCES:
        raise ValueError(f"unexpected status/confidence in reply: {text[:200]!r}")
    job = None
    if data.get("row") not in (None, "", "null", "none"):
        try:
            number = int(str(data["row"]).lower().replace("row", "").strip())
        except ValueError:
            raise ValueError(f"unexpected row in reply: {data.get('row')!r}")
        job = next((j for j in jobs if any(r["row"] == number for r in j["rows"])), None)
    return {"status": status, "job": job, "confidence": confidence,
            "reason": " ".join(str(data.get("reason", "")).split())}


def call_claude(user_message, model=STATUS_EMAIL_MODEL, thinking=STATUS_EMAIL_THINKING):
    """One classification call. Returns (reply text, usage dict)."""
    extra = {"thinking": thinking} if thinking else {}
    raw, usage = llm.complete(model, SYSTEM_PROMPT, [{"role": "user", "content": user_message}],
                              MAX_TOKENS, json_mode=True, purpose="status email call",
                              cache_system=True, **extra)          # REV-06
    if usage.stop_reason == "max_tokens":
        raise ValueError(f"reply cut at max_tokens ({MAX_TOKENS})")
    if usage.stop_reason == "refusal":
        raise ValueError(f"model declined (refusal, category {usage.refusal_category})")
    if not raw.strip():
        raise ValueError(f"empty reply (stop_reason {usage.stop_reason})")
    return raw, {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def cache_key(fields):
    """Cache key of one email: its Message-ID, hashed. Without one (rare),
    From + Date + Subject stand in."""
    ident = fields.get("message_id") or "\n".join(
        (fields["from"], fields["date"], fields["subject"]))
    return _sha(ident)


def prompt_hash(user_message, model=STATUS_EMAIL_MODEL, thinking=STATUS_EMAIL_THINKING):
    """Hash of everything a call depends on. Any change (a row's status or
    title, the candidate list, the model, the system prompt) is a cache miss."""
    return _sha("\n\0".join((model, json.dumps(thinking, sort_keys=True),
                             SYSTEM_PROMPT, user_message)))


def load_cache(path=None):
    """{key: {"prompt", "raw", "used"}} from the cache file; {} when absent.
    An unreadable file fails loudly: it is committed state, not a scratch file."""
    path = Path(path or CACHE_PATH)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_cache(cache, today=None, path=None, keep_days=LOOKBACK_DAYS):
    """Write the cache, dropping entries not used in the last keep_days days
    (their email has left the lookback). Returns the number pruned."""
    today = today or date.today()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    kept = {k: v for k, v in cache.items() if v.get("used", "") >= cutoff}
    write_json_atomic(str(path or CACHE_PATH), dict(sorted(kept.items())),
                      indent=1, sort_keys=True)
    return len(cache) - len(kept)


def title_matched(text, job):
    """Company + title match: the job's title appears in the email (the
    company already does, or it would not be a candidate)."""
    title = _normalize(job["row"]["title"])
    return bool(title) and title in text


def decide(result, text):
    """Which rows one classification writes.

    Returns (rows, why_not): rows to write, or [] with why_not saying why the
    status is only logged. For status none: ([], None). A job seen on several
    rows writes to its engaged rows (Interested or later); with none engaged
    it is logged, since there is no telling which copy was applied through.
    """
    status, job = result["status"], result["job"]
    if status == STATUS_NONE:
        return [], None
    if job is None:
        return [], "no single candidate row"
    if result["confidence"] != CONFIDENCE_TO_WRITE:
        return [], f"confidence {result['confidence']}"
    if status == STATUS_REJECTED and not title_matched(text, job):
        return [], "Rejected, but row was not matched by company + title"
    engaged = [r for r in job["rows"] if _rank(r) >= 1]
    if not engaged and not title_matched(text, job):
        # A row nobody has acted on, named only by company: the company name
        # may be an ordinary word ("Next Steps" -> Step, 19 Oct replay).
        return [], "row not engaged and title not in the email"
    if len(job["rows"]) == 1:
        return list(job["rows"]), None
    if not engaged:
        return [], f"{len(job['rows'])} rows of this job, none engaged"
    return engaged, None


def _row_desc(row):
    return f"{row['company']} — {row['title'][:50]}"


def _search_message_ids(mail, since_date):
    """UID of every INBOX message since the date. Filtering is the company prefilter;
    no keyword search, which missed acknowledgments like "we've received your
    resume"."""
    status, ids = mail.uid("SEARCH", None, f"ALL SINCE {since_date}")
    if status != "OK":
        raise RuntimeError(f"Gmail search failed: {status}")
    return ids[0].split()


FETCH_BATCH = 100
# Bytes fetched per message. Text parts precede attachments, and the 14-day
# inbox (~1,200 emails, ~230 MB on 19 Oct) has outliers past 25 MB.
FETCH_MAX_BYTES = 512 * 1024


def _fetch_messages(connect, ids):
    """Raw bytes of each message (its first FETCH_MAX_BYTES), FETCH_BATCH at
    a time: one round trip per batch. connect() opens a selected mailbox.

    imaplib occasionally loses its place in a large FETCH reply and aborts the
    connection (19 Oct replay, mail arriving mid-run); the batch is retried
    once on a fresh connection, and a second abort fails the pass loudly."""
    mail = connect()
    try:
        for i in range(0, len(ids), FETCH_BATCH):
            batch = b",".join(ids[i:i + FETCH_BATCH]).decode()
            try:
                status, data = mail.uid("FETCH", batch, f"(BODY.PEEK[]<0.{FETCH_MAX_BYTES}>)")
            except imaplib.IMAP4.abort as e:
                print(f"[StatusEmail] IMAP connection aborted on fetch ({e}); reconnecting, retrying batch")
                mail = connect()
                status, data = mail.uid("FETCH", batch, f"(BODY.PEEK[]<0.{FETCH_MAX_BYTES}>)")
            if status != "OK":
                raise RuntimeError(f"Gmail fetch failed: {status}")
            for item in data or []:
                if isinstance(item, tuple) and len(item) > 1:
                    yield item[1]
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def check_status_emails(dry_run=False, lookback_days=LOOKBACK_DAYS):
    """Full pass: scan inbox, prefilter, classify with Claude, update Status cells.
    dry_run: print every decision and the writes it would make; write nothing."""
    gmail_address = os.environ["GMAIL_ADDRESS"].strip()
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"].strip()

    rows = sheets.get_index_rows()
    if not rows:
        print("[StatusEmail] Index empty or unreadable — skipping")
        return

    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    def connect():
        mail = imaplib.IMAP4_SSL(GMAIL_IMAP_SERVER, GMAIL_IMAP_PORT)
        mail.login(gmail_address, gmail_app_password)
        mail.select("INBOX", readonly=True)
        return mail

    # UIDs, not sequence numbers: archiving mail during the run renumbers the
    # inbox, and the fetch may reconnect.
    mail = connect()
    ids = _search_message_ids(mail, since_date)
    mail.logout()
    print(f"[StatusEmail] {len(ids)} emails in INBOX since {since_date} "
          f"(model {STATUS_EMAIL_MODEL}{', DRY RUN' if dry_run else ''})")

    stats = {"candidates": 0, "calls": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0}
    candidate_days = {}
    candidates = []  # (fields, text, jobs) in inbox order
    for raw_msg in _fetch_messages(connect, ids):
        msg = email.message_from_bytes(raw_msg)
        fields = email_fields(msg)
        if parseaddr(fields["from"])[1].lower() == gmail_address.lower():
            continue
        text = email_text(fields)
        jobs = candidate_jobs(text, rows)
        if not jobs:
            continue
        candidates.append((fields, text, jobs))
        try:
            day = email.utils.parsedate_to_datetime(fields["date"]).date().isoformat()
        except Exception:
            day = "?"
        candidate_days[day] = candidate_days.get(day, 0) + 1
    stats["candidates"] = len(candidates)

    cache = load_cache()
    today = date.today().isoformat()
    stats["cached"] = 0

    def classify(candidate):
        """(raw, usage, error, cache key, prompt hash); usage None = cache hit."""
        fields, _, jobs = candidate
        user_message = build_user_message(fields, jobs)
        key, prompt = cache_key(fields), prompt_hash(user_message)
        hit = cache.get(key)
        if hit and hit.get("prompt") == prompt:
            return hit["raw"], None, None, key, prompt
        try:
            raw, usage = call_claude(user_message)
            return raw, usage, None, key, prompt
        except Exception as e:
            return None, None, e, key, prompt

    # Calls are independent; replies are handled below in inbox order, so the
    # last forward transition per row still wins as before.
    with ThreadPoolExecutor(max_workers=STATUS_EMAIL_MAX_IN_FLIGHT) as pool:
        replies = list(pool.map(classify, candidates))

    updates = {}  # row number -> write details (last forward transition wins)
    for (fields, text, jobs), (raw, usage, error, key, prompt) in zip(candidates, replies):
        email_desc = (f"email {fields['date']!r} / {fields['subject'][:70]!r} "
                      f"/ From {fields['from']!r}")
        try:
            if error:
                raise error
            if usage is None:
                stats["cached"] += 1
            else:
                stats["calls"] += 1
                stats["input_tokens"] += usage["input_tokens"]
                stats["output_tokens"] += usage["output_tokens"]
            result = parse_response(raw, jobs)
            # Only a reply that parsed is kept; a failure is retried next run.
            cache[key] = {"prompt": prompt, "raw": raw, "used": today}
        except ApiAccessError:
            raise
        except Exception as e:
            # Transient or malformed: the email is still in the lookback, so
            # the next run classifies it again.
            stats["failed"] += 1
            print(f"[StatusEmail] CLASSIFY FAILED ({type(e).__name__}: {e}): {email_desc}")
            continue

        job, target = result["job"], result["status"]
        print(f"[StatusEmail] DECISION {target} ({result['confidence']}) "
              f"row {job['row']['row'] if job else '-'} — {result['reason']} | {fields['subject'][:80]!r}")
        write_rows, why_not = decide(result, text)
        if target == STATUS_NONE:
            continue
        if not write_rows:
            where = f" ({_row_desc(job['row'])})" if job else ""
            print(f"[StatusEmail] UNMATCHED {target} ({why_not}{where}): {email_desc}")
            continue
        for row in write_rows:
            current = updates[row["row"]]["new"] if row["row"] in updates else row["status"]
            if _RANK.get(target, 0) <= _RANK.get(current, 99):
                # Not a forward transition (or current status is manual/terminal
                # like Declined/Closed, which rank as 99) — leave it alone.
                print(f"[StatusEmail] SKIP Row {row['row']} ({_row_desc(row)}): "
                      f"already {current or 'New'}, email says {target}")
                continue
            updates[row["row"]] = {"old": row["status"], "new": target, "row": row,
                                   "reason": result["reason"], "email": email_desc}

    for n, u in updates.items():
        print(f"[StatusEmail] {'DRY RUN would WRITE' if dry_run else 'WRITE'} Row {n} "
              f"({_row_desc(u['row'])}): {u['old'] or 'New'} -> {u['new']} "
              f"[{u['reason']}] {u['email']}")

    print(f"[StatusEmail] {stats['candidates']} candidate emails, {stats['cached']} from cache, "
          f"{stats['calls']} Claude calls ({stats['failed']} failed), {stats['input_tokens']:,} input / "
          f"{stats['output_tokens']:,} output tokens")
    if not dry_run:
        # Before the Sheet write: a failed write re-decides from the cache next
        # run, and forward-only writes make that safe.
        pruned = save_cache(cache)
        print(f"[StatusEmail] Decision cache: {len(cache) - pruned} entries "
              f"({pruned} pruned, not used in {LOOKBACK_DAYS} days)")
    if dry_run:
        days = max(len(candidate_days), 1)
        print(f"[StatusEmail] DRY RUN: nothing written. Candidates per day with mail: "
              f"{stats['candidates'] / days:.1f} over {len(candidate_days)} days "
              f"({lookback_days}-day window: {stats['candidates'] / lookback_days:.1f}/day)")
        return
    if updates:
        n = sheets.update_statuses([(r, u["new"]) for r, u in updates.items()])
        print(f"[StatusEmail] Updated {n} row(s)")
    else:
        print("[StatusEmail] No status updates")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Application-status tracking from Gmail (PRD §15)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every decision and would-be write; write nothing")
    parser.add_argument("--days", type=int, default=None,
                        help=f"lookback in days (default {LOOKBACK_DAYS}; "
                             f"{STATUS_EMAIL_DRY_RUN_DAYS} with --dry-run)")
    args = parser.parse_args()
    days = args.days or (STATUS_EMAIL_DRY_RUN_DAYS if args.dry_run else LOOKBACK_DAYS)
    check_status_emails(dry_run=args.dry_run, lookback_days=days)
    sys.exit(0)
