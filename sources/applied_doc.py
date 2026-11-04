# ============================================================
# sources/applied_doc.py — Seed the tracker from the Job Index Doc (PRD §16)
# ============================================================
#
# Every entry in the job-search-index Google Doc is a job the candidate
# applied to. This module parses the doc (entry boundary = HEADING_1
# shaped "{n} {Company} - {Title}"; numbering has gaps; interior
# headings like "Overview" are noise), matches entries to main-index
# rows by company + title (same key as the Gmail status feature), and:
#   - appends missing entries with Status "Applied" + applied date
#   - promotes existing rows to "Applied" only if below it (never
#     downgrades Interviewing/Rejected/Declined/Closed)
#   - fills "Applied Date" where empty
#   - writes "Status Date" (a date cell) with every Status it sets (DEL-12)
#
# Token cost control: entries whose company+title already key an
# Applied-or-later row are skipped without a Claude call — the cheap
# heading parse handles the match, so steady-state runs cost ~nothing.
# Claude extraction (company, title, applied_date) runs only for
# entries the cheap parse cannot match to a row.

import os
import re
import sys
import json
from datetime import datetime

import llm
from google.oauth2 import service_account
from googleapiclient.discovery import build

import sheets
import profile_loader
from sources.status_email import _RANK, _normalize, STATUS_RECEIVED
from state import company_names

SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]

# Entry header: "{n} {Company} - {Title}" as HEADING_1; numbering non-sequential
# PUB-08: this step creates Applied rows in the Sheet, so it gets the scoring
# model (profile.yaml scoring.model); the utility model (config.PROFILE_MODEL)
# is for steps that are cheap to get wrong.
MODEL = profile_loader.active()["scoring"]["model"]

ENTRY_HEADER_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
SNIPPET_CHARS = 1600  # heading + Applied line + JD opening is plenty

_DATE_TOKEN_RE = re.compile(
    r"([A-Za-z]{3,9}\.?\s+\d{1,2}(?:,?\s*\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
)
_DATE_FORMATS = ["%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y",
                 "%m/%d/%Y", "%m/%d/%y"]
_DATE_FORMATS_NO_YEAR = ["%B %d", "%b %d", "%m/%d"]


def parse_applied_date(text):
    """Lenient date from an 'Applied ...' line. Returns ISO date or ''.

    Handles with/without dash, with/without year (year-less dates assume the
    most recent occurrence not in the future), trailing whitespace.
    """
    for line in text.splitlines():
        m = _APPLIED_LINE_RE.match(line)
        if not m:
            continue
        # TRK-10: search after the word "Applied", and try every date-shaped
        # token. In "Applied 10/6/2026" the month-name pattern used to match
        # "Applied 10" first, fail, and leave the date blank.
        # A failed token is retried one character on, not after its end, so
        # "LinkedIn 10/6/2026" does not resume at "6/20".
        pos = m.end()
        while (token_match := _DATE_TOKEN_RE.search(line, pos)):
            parsed = _parse_date_token(token_match.group(1))
            if parsed:
                return parsed
            pos = token_match.start() + 1
    return ""


_APPLIED_LINE_RE = re.compile(r"\s*applied\b", re.I)


def _parse_date_token(raw):
    """ISO date for one date-shaped token, or '' if it is not a date."""
    token = " ".join(raw.replace(".", "").split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            d = datetime.strptime(token, fmt)
        except ValueError:
            continue
        today = datetime.utcnow()
        d = d.replace(year=today.year)
        if d > today:
            d = d.replace(year=today.year - 1)
        return d.strftime("%Y-%m-%d")
    return ""


def _docs_service():
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES)
    return build("docs", "v1", credentials=creds)


def _paragraph_text(element):
    para = element.get("paragraph")
    if not para:
        return ""
    return "".join(r.get("textRun", {}).get("content", "")
                   for r in para.get("elements", []))


def read_entries(doc_id=None):
    """Split the index doc into entries: [{heading, body}] (body truncated)."""
    # PUB-05: the index doc is a per-user resource ID, so it comes from the
    # environment only (GOOGLE_DOC_ID, an Actions secret), never from code.
    doc_id = doc_id or os.environ.get("GOOGLE_DOC_ID", "").strip()
    if not doc_id:
        raise RuntimeError("GOOGLE_DOC_ID is not set: the Job Index Doc's ID must come from the environment")
    doc = _docs_service().documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])

    entries = []
    current = None
    for element in content:
        para = element.get("paragraph")
        if not para:
            continue
        text = _paragraph_text(element)
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        if style == "HEADING_1" and ENTRY_HEADER_RE.match(text):
            if current:
                entries.append(current)
            current = {"heading": text.strip(), "body": ""}
        elif current and len(current["body"]) < SNIPPET_CHARS:
            current["body"] += text
    if current:
        entries.append(current)
    return entries


# TRK-10: heading shapes the plain "{n} {Company} - {Title}" split got wrong.
_STATUS_WORDS = ("applied", "rejected", "interviewing", "declined", "closed")
_TRAILING_STATUS_RE = re.compile(r"\s+-\s+(?:" + "|".join(_STATUS_WORDS) + r")\s*$", re.I)
_PLACEHOLDER_NAMES = {"unknown", "confidential", "n a", "tbd"}
# Where a title starts when a heading has no " - " between company and title.
_TITLE_START_RE = re.compile(
    r"\b(?:Senior Vice President|Executive Vice President|Vice President|SVP|EVP|VP|"
    r"Senior Director|Sr\.? Director|Executive Director|Managing Director|Director|"
    r"Head of|Chief|Deputy|Senior Manager|Manager)\b")


def cheap_parse(heading):
    """(company, title) from '{n} {Company} - {Title}', splitting on the first
    ' - ' outside square brackets. Titles containing dashes parse fine.

    TRK-10 shapes, each seen in the live doc:
      - "15 - Acme ..."   a dash after the number is dropped;
      - "... - Rejected"  a trailing status word is dropped (§16 only ever
                          promotes to Applied; the status is not read here);
      - "[unknown - covenant HR] - VP, IT"  a bracketed company keeps its inner
                          dash; placeholder parts (unknown, confidential) are
                          dropped, so the company is "covenant HR";
      - "Acme Vice President, IT"  no separator at all: the title starts at the
                          first seniority word after the company's first word.
    Anything still unsplit returns (rest, "") and falls through to Claude."""
    m = ENTRY_HEADER_RE.match(heading)
    if not m:
        return "", ""
    rest = re.sub(r"^-\s*", "", m.group(2).strip())
    rest = _TRAILING_STATUS_RE.sub("", rest).strip()

    if rest.startswith("[") and "]" in rest:
        close = rest.index("]")
        inner, after = rest[1:close], rest[close + 1:]
        parts = [p.strip() for p in re.split(r"\s+[-|]\s+", inner) if p.strip()]
        named = [p for p in parts if _normalize(p) not in _PLACEHOLDER_NAMES]
        company = " - ".join(named) if len(parts) > 1 else rest[:close + 1]
        title = re.sub(r"^\s*-\s*", "", after).strip()
        return company.strip(), title

    company, sep, title = rest.partition(" - ")
    if sep:
        return company.strip(), title.strip()
    start = _TITLE_START_RE.search(rest, rest.find(" ") + 1 if " " in rest else len(rest))
    if start:
        return rest[:start.start()].strip(), rest[start.start():].strip()
    return rest.strip(), ""


def _find_row(rows, company, title):
    """Match by normalized company equality + title equality-or-containment."""
    nc, nt = _normalize(company), _normalize(title)
    if not nc:
        return None
    # Employer or, behind a via tail, the poster (FIL-13).
    cands = [r for r in rows if nc in {_normalize(n) for n in company_names(r["company"])}]
    if not nt:
        return cands[0] if len(cands) == 1 else None
    for r in cands:
        rt = _normalize(r["title"])
        if rt and (rt == nt or nt in rt or rt in nt):
            return r
    return None


def claude_extract(heading, body):
    """Ask Claude for (company, title, applied_date) from one doc entry."""
    prompt = f"""This is one entry from a job-application index document. The first line is
shaped "<number> <Company> - <Job Title>" (the title itself may contain dashes),
and an "Applied ..." line may carry the application date in a loose format.

Entry:
{heading}
{body[:SNIPPET_CHARS]}

Return ONLY valid JSON, no prose:
{{"company": "<company name>", "title": "<job title>", "applied_date": "<YYYY-MM-DD or empty string if no date is stated>"}}"""
    raw, _ = llm.complete(MODEL, None, [{"role": "user", "content": prompt}], 200,
                          json_mode=True, purpose="Applied-seed extraction")
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)
    return (str(data.get("company", "")).strip(),
            str(data.get("title", "")).strip(),
            str(data.get("applied_date", "")).strip())


def seed_from_doc(dry_run=False):
    """Full pass: read doc, match/skip, extract new entries, write the sheet.
    dry_run: log every write it would make and make none (TRK-10 check)."""
    if not dry_run:
        sheets.ensure_columns(["Status", "Applied Date", "Status Date"])
    rows = sheets.get_index_rows()
    entries = read_entries()
    print(f"[AppliedDoc] {len(entries)} entries in index doc, {len(rows)} sheet rows")

    applied_rank = _RANK[STATUS_RECEIVED]
    appended = promoted = dated = skipped = 0

    for e in entries:
        company, title = cheap_parse(e["heading"])
        row = _find_row(rows, company, title)
        date = parse_applied_date(e["body"])

        if row and _RANK.get(row["status"], 99) >= applied_rank:
            # Already Applied-or-later (or manual/terminal) — no Claude call.
            if date and not row.get("applied_date"):
                if dry_run:
                    print(f"[AppliedDoc] DRY RUN would fill Applied Date {date} on row {row['row']} "
                          f"({row['company']} — {row['title'][:50]})")
                else:
                    sheets.update_row_fields(row["row"], {"Applied Date": date})
                row["applied_date"] = date
                dated += 1
            skipped += 1
            continue

        if row is None:
            # Cheap parse couldn't key a row — let Claude read the entry.
            try:
                company, title, cdate = claude_extract(e["heading"], e["body"])
                date = date or cdate
            except llm.ApiAccessError:
                raise        # OPS-12: every entry would fail the same way; stop the seed
            except Exception as ex:
                print(f"[AppliedDoc] Claude extraction failed for {e['heading'][:60]!r}: "
                      f"{ex} — using heading parse")
            row = _find_row(rows, company, title)

        if row:
            fields = {}
            if _RANK.get(row["status"], 99) < applied_rank:
                fields["Status"] = STATUS_RECEIVED
                fields["Status Date"] = sheets.status_date()  # DEL-12
                row["status"] = STATUS_RECEIVED
                promoted += 1
            if date and not row.get("applied_date"):
                fields["Applied Date"] = date
                row["applied_date"] = date
                dated += 1
            if fields:
                if not dry_run:
                    sheets.update_row_fields(row["row"], fields)
                print(f"[AppliedDoc] {'DRY RUN would update' if dry_run else 'Updated'} row {row['row']}: {fields} "
                      f"({company} — {title[:50]})")
        else:
            if not company:
                print(f"[AppliedDoc] SKIPPED unparseable entry: {e['heading'][:80]!r}")
                continue
            new_row = 0 if dry_run else sheets.append_applied_row(company, title, date)
            rows.append({"row": new_row, "company": company, "title": title,
                         "status": STATUS_RECEIVED, "applied_date": date})
            appended += 1
            print(f"[AppliedDoc] {'DRY RUN would append' if dry_run else 'Appended'}: {company} — {title[:50]} (applied {date or '?'})")

    print(f"[AppliedDoc] Done: {appended} appended, {promoted} promoted to Applied, "
          f"{dated} dates filled, {skipped} already Applied-or-later")


if __name__ == "__main__":
    seed_from_doc(dry_run="--dry-run" in sys.argv[1:])
