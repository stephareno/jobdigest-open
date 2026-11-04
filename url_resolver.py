# url_resolver.py — follow redirects on aggregator URLs to get the direct apply link
#
# Priority order per CLAUDE.md:
#   company site > LinkedIn > Indeed/ZipRecruiter > aggregator (flagged)

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests
from state import normalize_url

_TIMEOUT = 6
# PUB-21: redirects are followed by hand, at most this many, and every hop is
# checked (scheme and address) before it is requested.
MAX_REDIRECTS = 5
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Patterns that indicate a direct company career page (not an aggregator)
_AGGREGATOR_DOMAINS = re.compile(
    r"(adzuna\.com|jsearch|indeed\.com|ziprecruiter\.com|glassdoor\.com"
    r"|monster\.com|careerbuilder\.com|simplyhired\.com|dice\.com"
    r"|snagajob\.com|theladders\.com|lensa\.com|talentify\.io"
    r"|jobgether\.com|jooble\.org|zippia\.com|salary\.com)",
    re.IGNORECASE,
)


_ATS_HOSTS = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkdaysite\.com"
    r"|smartrecruiters\.com|rippling\.com|workable\.com|recruitee\.com|bamboohr\.com"
    r"|breezy\.hr|teamtailor\.com|personio\.(de|com)|oraclecloud\.com|icims\.com"
    r"|taleo\.net|successfactors\.(com|eu)|avature\.net|jobvite\.com|eightfold\.ai"
    r"|paylocity\.com|paycomonline\.net|ultipro\.com|adp\.com|dayforcehcm\.com"
    r"|applytojob\.com|jazzhr\.com|pinpointhq\.com"
    r"|saashr\.com|gem\.com|comeet\.com|comeet\.co|careers-page\.com|jobscore\.com)$",
    re.IGNORECASE,
)
_JOB_BOARD_HOSTS = re.compile(
    r"(indeed\.com|ziprecruiter\.com|builtin\.com|builtin[a-z]+\.com|wellfound\.com"
    r"|angel\.co|glassdoor\.com|monster\.com|dice\.com|careersingovernment\.com"
    r"|usajobs\.gov|simplyhired\.com|themuse\.com|jobright\.ai|ycombinator\.com"
    r"|workatastartup\.com|otta\.com|welcometothejungle\.com)$",
    re.IGNORECASE,
)

# Lower is better: company site / ATS > LinkedIn > job boards > aggregators.
LINK_RANK = {"ats": 0, "company": 0, "linkedin": 1, "job board": 2, "aggregator": 3, "none": 4}


def link_class(url):
    """ats | company | linkedin | job board | aggregator | none."""
    url = (url or "").strip()
    if not url:
        return "none"
    host = (urlsplit(url if "://" in url else f"https://{url}").hostname or "").lower()
    if not host:
        return "none"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return "linkedin"
    if _ATS_HOSTS.search(host):
        return "ats"
    if _JOB_BOARD_HOSTS.search(host):
        return "job board"
    if _AGGREGATOR_DOMAINS.search(url):
        return "aggregator"
    return "company"


def best_link(urls):
    """The highest-priority link of several; the first wins a tie. "" if none."""
    urls = [u for u in urls if u]
    return min(urls, key=lambda u: LINK_RANK[link_class(u)]) if urls else ""


def _resolve_host(host):
    """Every IP address the host resolves to (a seam for tests)."""
    return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def refuse_reason(url):
    """PUB-21: why this URL must not be requested, or "" if it may be. Only
    http and https; the host must resolve, and to public addresses only
    (no loopback, private, link-local, multicast, reserved or unspecified).
    The check runs before every request, each redirect hop included."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable URL"
    if parts.scheme.lower() not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'} not allowed"
    host = parts.hostname
    if not host:
        return "no host"
    try:
        addresses = _resolve_host(host)
    except (socket.gaierror, UnicodeError, OSError) as e:
        return f"host does not resolve ({type(e).__name__})"
    if not addresses:
        return "host does not resolve"
    for raw in addresses:
        addr = ipaddress.ip_address(raw.split("%")[0])
        for flag in ("is_loopback", "is_private", "is_link_local", "is_multicast", "is_reserved",
                     "is_unspecified"):
            if getattr(addr, flag):
                return f"{host} resolves to {addr} ({flag[3:].replace('_', '-')})"
    return ""


def _request(url):
    """One hop, never following a redirect: HEAD, or GET if HEAD fails."""
    try:
        return requests.head(url, allow_redirects=False, timeout=_TIMEOUT, headers=_HEADERS)
    except Exception:
        return requests.get(url, allow_redirects=False, timeout=_TIMEOUT, stream=True, headers=_HEADERS)


def resolve_apply_link(url):
    """
    Follow redirects and return the final URL, normalized.
    Falls back to the original URL on any error.
    Adds 'link_resolved' bool and 'link_is_aggregator' bool to help flag
    unresolved aggregator links in the sheet.

    PUB-21: redirects are followed one hop at a time, at most MAX_REDIRECTS,
    and each URL is checked by refuse_reason before it is requested. A link
    refused at any hop, or one that redirects more than MAX_REDIRECTS times,
    is stored as given (unresolved) with a log line; the refused address is
    never requested.
    """
    if not url:
        return url, False, False

    unresolved = (normalize_url(url), False, bool(_AGGREGATOR_DOMAINS.search(url)))
    current = url.strip()
    try:
        for hop in range(MAX_REDIRECTS + 1):
            reason = refuse_reason(current)
            if reason:
                where = "" if hop == 0 else f" (redirect {hop} to {current})"
                print(f"[URLResolver] not followed: {reason}{where}: {url}")
                return unresolved
            resp = _request(current)
            location = resp.headers.get("Location") if 300 <= resp.status_code < 400 else None
            if not location:
                final = normalize_url(current)
                resolved = final.lower() != normalize_url(url).lower()
                return final, resolved, bool(_AGGREGATOR_DOMAINS.search(final))
            current = urljoin(current, location)
        print(f"[URLResolver] not followed: more than {MAX_REDIRECTS} redirects: {url}")
        return unresolved
    except Exception:
        return unresolved


def resolve_jobs(jobs):
    """Resolve apply links for a list of job dicts in place."""
    total = len(jobs)
    for i, job in enumerate(jobs):
        raw = job.get("apply_link", "")
        if not raw:
            continue
        final, resolved, is_aggregator = resolve_apply_link(raw)
        job["apply_link"] = final
        job["link_is_aggregator"] = is_aggregator
        if (i + 1) % 10 == 0:
            print(f"[URLResolver] Resolved {i+1}/{total}")
    return jobs
