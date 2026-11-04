# company_lists.py — the company lists, the public input to sourcing (PUB-11)
#
# A list is a CSV in data/company_lists/ with a header row. `name` is the only
# required column; `website` and `notes` may be empty or absent. Other
# columns are kept and read by the filters (sic, state) or, when a row
# carries `ats` and `url`, taken as a board the engine can fetch as it is.
# Lines starting with "#" are comments (a build tool's provenance line).
#
# The profile's sources.company_lists names the files in order, each with a
# tier and an optional filter:
#   dream  the dream-company threshold and the ATS/watchlist path
#          (profile_loader.dream_companies adds these names)
#   watch  the board tier (sources/startups.load_boards adds these boards)
# A company in several lists takes the tier of the first file that lists it.
#
# PUB-12: a watch entry may also name `boards:`, a shipped file of boards the
# maintainer pre-resolved for that list (name, ats, url, website; e.g.
# starter_boards.csv). Its boards for the companies the entry's filter keeps
# come first, ahead of boards the list rows carry. Rows with ats
# `careers_page` are kept in the file for coverage but not fetched as boards.
# A list row with an `outcome` (board | workday | careers_page |
# unsupported:<vendor> | none) is pre-resolved: `main.py resolve` skips it.
#
# PUB-13: data/company_lists/corrections.csv (name, field, value, reason) holds
# hand corrections, which win over every list and build: field is website,
# board (value "<ats> <url>", empty to remove it) or outcome.
#
# PUB-15: the shipped starter list (starter_companies.csv) holds both segments,
# `public` (SEC EDGAR) and `startup`, in a `segment` column the filter reads.
# A startup row carries no SIC code, so a `sic` filter narrows the public
# segment only; `states` tests every row.
#
# Standard library only (profile_loader imports it).

import csv
import os
import re
from collections import namedtuple
from urllib.parse import parse_qs, unquote, urlsplit

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LISTS_DIR = os.path.join(REPO_ROOT, "data", "company_lists")
CORRECTIONS = os.path.join(LISTS_DIR, "corrections.csv")
CORRECTION_FIELDS = ("website", "board", "outcome")
# Outcomes that leave a company a row in a boards file.
BOARD_OUTCOMES = ("board", "workday", "careers_page")
TIERS = ("dream", "watch")
FILTER_KEYS = ("segment", "sic", "states", "names", "exclude_names")
SEGMENTS = ("public", "startup")
SIC_RE = re.compile(r"\d{4}(?:-\d{4})?")

Company = namedtuple("Company", "name website notes tier file row")

csv.field_size_limit(10 ** 9)


def _full(path):
    return path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)


def read_list(path):
    """[row dict] from one list file, every value stripped. Raises
    FileNotFoundError, or ValueError when there is no `name` column."""
    with open(_full(path), newline="", encoding="utf-8") as fh:
        lines = [line for line in fh if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if "name" not in (reader.fieldnames or []):
        raise ValueError(f"{path}: the header row must have a `name` column (got {reader.fieldnames})")
    rows = []
    for row in reader:
        row = {k: (v or "").strip() for k, v in row.items() if k}
        if row.get("name"):
            rows.append(row)
    return rows


def load_corrections(path=None):
    """{name casefold: {"name": name, field: value}} from corrections.csv; {} when
    it is absent. An unknown field stops the run."""
    path = path or CORRECTIONS
    if not os.path.exists(path):
        return {}
    out = {}
    for row in read_list(path):
        if row.get("field") not in CORRECTION_FIELDS:
            raise ValueError(f"{path}: {row['name']}: field must be one of {', '.join(CORRECTION_FIELDS)}, "
                             f"got {row.get('field')!r}")
        out.setdefault(row["name"].casefold(), {"name": row["name"]})[row["field"]] = row.get("value", "")
    return out


def _board_value(value):
    """(ats, url) from a `board` correction's "<ats> <url>" value."""
    ats, _, url = value.partition(" ")
    return ats, url.strip()


def apply_corrections(rows, corrections=None):
    """Apply corrections to list rows in place, last, so they win; returns rows."""
    corrections = load_corrections() if corrections is None else corrections
    for row in rows:
        fix = corrections.get(row["name"].casefold())
        if not fix:
            continue
        if "website" in fix:
            row["website"] = fix["website"]
            if "website_source" in row:
                row["website_source"] = "correction"
        if "board" in fix:
            row["ats"], row["url"] = _board_value(fix["board"])
            if "outcome" in row:
                row["outcome"] = ("workday" if row["ats"] == "workday" else "board") if row["ats"] else "none"
        if "outcome" in fix:
            row["outcome"] = fix["outcome"]
            if fix["outcome"].split(":")[0] not in BOARD_OUTCOMES:
                row["ats"], row["url"] = "", ""
    return rows


def correct_boards(boards, corrections=None):
    """A boards file's rows (name, ats, url, website) with corrections applied:
    a company corrected to no board loses its rows, a corrected board replaces them."""
    corrections = load_corrections() if corrections is None else corrections
    out = []
    for b in boards:
        fix = corrections.get(b["name"].casefold(), {})
        if "board" in fix or ("outcome" in fix and fix["outcome"].split(":")[0] not in BOARD_OUTCOMES):
            continue
        out.append({**b, "website": fix["website"]} if "website" in fix else b)
    for fix in corrections.values():
        ats, url = _board_value(fix.get("board", ""))
        if ats and url:
            out.append({"name": fix["name"], "ats": ats, "url": url, "website": fix.get("website", "")})
    return out


def _sic_ranges(values):
    out = []
    for v in values:
        lo, _, hi = str(v).partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def matches(row, flt):
    """Whether a list row passes an entry's filter (None or {} passes all).
    names: only these exact names (case-insensitive); exclude_names: never
    these; segment: the row's segment is one of these; sic: the row's sic is
    in one of the codes or ranges (a startup row is not tested: it has no SIC
    code); states: the row's state is one of these. A row with no segment,
    sic or state fails that test."""
    if not flt:
        return True
    name = row.get("name", "").casefold()
    if flt.get("exclude_names") and name in {n.casefold() for n in flt["exclude_names"]}:
        return False
    if flt.get("names") and name not in {n.casefold() for n in flt["names"]}:
        return False
    if flt.get("segment") and row.get("segment", "") not in flt["segment"]:
        return False
    if flt.get("sic") and row.get("segment") != "startup":
        sic = row.get("sic", "")
        if not sic.isdigit() or not any(lo <= int(sic) <= hi for lo, hi in _sic_ranges(flt["sic"])):
            return False
    if flt.get("states"):
        if row.get("state", "").upper() not in {s.upper() for s in flt["states"]}:
            return False
    return True


def load(entries):
    """[Company] from the profile's sources.company_lists, in file order,
    filters applied, each name once (the first file that lists it wins)."""
    out, seen = [], set()
    corrections = load_corrections()
    for entry in entries or ():
        for row in apply_corrections(read_list(entry["file"]), corrections):
            key = row["name"].casefold()
            if key in seen or not matches(row, entry.get("filter")):
                continue
            seen.add(key)
            out.append(Company(row["name"], row.get("website", ""), row.get("notes", ""),
                               entry["tier"], entry["file"], row))
    return out


def dream_names(entries):
    return [c.name for c in load(entries) if c.tier == "dream"]


# --- Boards carried in a list (name, ats, url) ---------------------------------------

_PATH_TOKEN = {
    "greenhouse": r"/v1/boards/([^/?#]+)",
    "ashby": r"/job-board/([^/?#]+)",
    "lever": r"/v0/postings/([^/?#]+)",
    "rippling": r"/board/([^/?#]+)",
    "smartrecruiters": r"/companies/([^/?#]+)",
    "workable": r"/accounts/([^/?#]+)",
    "gem": r"/job_board/v0/([^/?#]+)",
    "manatal": r"/career-page/([^/?#]+)",
    "jobscore": r"/jobs/([^/?#]+)",
}
_SUBDOMAIN_TOKEN = {"bamboohr": ("bamboohr.com",), "breezy": ("breezy.hr",), "recruitee": ("recruitee.com",),
                    "personio": ("jobs.personio.de", "jobs.personio.com"), "teamtailor": ("teamtailor.com",)}


def board_token(ats, url):
    """The board token the engine keys a board by, read from its URL (the
    inverse of tools/resolve_ats.board_api_url, plus Workday's page URL).
    None when the URL does not have the vendor's shape."""
    u = urlsplit(url if "://" in url else "https://" + url)
    host, path, query = u.netloc, u.path, parse_qs(u.query)
    if ats in _PATH_TOKEN:
        m = re.search(_PATH_TOKEN[ats], path)
        return unquote(m.group(1)) if m else None
    if ats in _SUBDOMAIN_TOKEN:
        for suffix in _SUBDOMAIN_TOKEN[ats]:
            if host.endswith("." + suffix):
                return host[:-len(suffix) - 1]
        # A Teamtailor career site on the company's own domain is keyed by host.
        return host if ats == "teamtailor" and host else None
    if ats == "workday":
        parts = [p for p in path.split("/") if p]
        return f"{unquote(host.split('.')[0])}:{unquote(parts[-1])}" if parts else None
    if ats == "adp":
        return (query.get("cid") or [None])[0]
    if ats == "oracle_hcm":
        m = re.search(r"siteNumber=([^,&;]+)", unquote(u.query))
        return f"{host}:{m.group(1)}" if m else None
    if ats == "ukg_ready":
        m = re.search(r"/companies/(?:%7C|\|)(\d+)", url)
        return f"{host}:{m.group(1)}" if m else None
    if ats == "comeet":
        m = re.search(r"/company/([^/]+)/positions", path)
        return f"{m.group(1)}:{query['token'][0]}" if m and query.get("token") else None
    return None


def boards(entries):
    """Board dicts (company, ats, slug, board_url) from watch-tier rows that
    carry ats and url, in list order -- an entry's `boards:` file first, for
    the names its filter keeps. A company may carry several boards in its own
    list; a name an earlier list already claims is skipped (the first file
    wins). A row whose URL does not parse is reported and skipped."""
    out, claimed = [], set()
    corrections = load_corrections()
    for entry in entries or ():
        rows = [r for r in apply_corrections(read_list(entry["file"]), corrections)
                if matches(r, entry.get("filter"))]
        names = {r["name"].casefold() for r in rows}
        if entry["tier"] == "watch" and entry.get("boards"):
            rows = [r for r in correct_boards(read_list(entry["boards"]), corrections)
                    if r["name"].casefold() in names and r.get("ats") != "careers_page"] + rows
        if entry["tier"] == "watch":
            for row in rows:
                ats, url = row.get("ats", ""), row.get("url", "")
                if row["name"].casefold() in claimed or not (ats and url):
                    continue
                slug = board_token(ats, url)
                if not slug:
                    print(f"[Lists] {entry['file']}: {row['name']}: cannot read a {ats} board from "
                          f"{url!r} — skipped")
                    continue
                out.append({"company": row["name"], "ats": ats, "slug": slug, "board_url": url})
        claimed |= names
    return out


# --- Validation (profile_loader) ------------------------------------------------------

def check_entries(value, path, errors, known_states=None):
    """sources.company_lists: a list of {file, tier, filter?}."""
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list of entries (file, tier, filter), got {type(value).__name__}")
        return
    for i, entry in enumerate(value):
        sub = f"{path}[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{sub}: must be a mapping with file and tier")
            continue
        for key in entry:
            if key not in ("file", "tier", "filter", "boards"):
                errors.append(f"{sub}.{key}: unknown field (an entry takes file, tier, filter and boards)")
        board_file = entry.get("boards")
        if board_file is not None:
            if not isinstance(board_file, str) or not board_file.endswith(".csv"):
                errors.append(f"{sub}.boards: a .csv path such as data/company_lists/starter_boards.csv")
            elif not os.path.isfile(_full(board_file)):
                errors.append(f"{sub}.boards: {board_file} does not exist")
            elif entry.get("tier") != "watch":
                errors.append(f"{sub}.boards: only a watch-tier entry reads a boards file")
        file = entry.get("file")
        if not isinstance(file, str) or not file.endswith(".csv"):
            errors.append(f"{sub}.file: required, a .csv path such as data/company_lists/my_companies.csv")
        elif not os.path.isfile(_full(file)):
            errors.append(f"{sub}.file: {file} does not exist")
        if entry.get("tier") not in TIERS:
            errors.append(f"{sub}.tier: must be dream or watch, got {entry.get('tier')!r}")
        flt = entry.get("filter")
        if flt is None:
            continue
        if not isinstance(flt, dict):
            errors.append(f"{sub}.filter: must be a mapping ({', '.join(FILTER_KEYS)})")
            continue
        for key, values in flt.items():
            fp = f"{sub}.filter.{key}"
            if key not in FILTER_KEYS:
                errors.append(f"{fp}: unknown filter (use {', '.join(FILTER_KEYS)})")
            elif not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
                errors.append(f"{fp}: must be a list of strings")
            elif key == "sic":
                bad = [v for v in values if not SIC_RE.fullmatch(v)
                       or (len(v) > 4 and int(v[:4]) > int(v[5:]))]
                if bad:
                    errors.append(f"{fp}: {bad} — each a four-digit code or a range such as \"7370-7379\"")
            elif key == "segment":
                bad = [v for v in values if v not in SEGMENTS]
                if bad:
                    errors.append(f"{fp}: {bad} — each one of {', '.join(SEGMENTS)}")
            elif key == "states" and known_states is not None:
                bad = [v for v in values if v.upper() not in known_states]
                if bad:
                    errors.append(f"{fp}: {bad} — two-letter US state codes")
