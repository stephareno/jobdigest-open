# url_resolver.py — follow redirects on aggregator URLs to get the direct apply link
#
# Priority order per CLAUDE.md:
#   company site > LinkedIn > Indeed/ZipRecruiter > aggregator (flagged)

import re
from urllib.parse import urlsplit

import requests
from state import normalize_url

_TIMEOUT = 6

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


def resolve_apply_link(url):
    """
    Follow redirects and return the final URL, normalized.
    Falls back to the original URL on any error.
    Adds 'link_resolved' bool and 'link_is_aggregator' bool to help flag
    unresolved aggregator links in the sheet.
    """
    if not url:
        return url, False, False

    try:
        # HEAD first (fast), fall back to GET if server rejects HEAD
        try:
            resp = requests.head(
                url, allow_redirects=True, timeout=_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"}
            )
        except Exception:
            resp = requests.get(
                url, allow_redirects=True, timeout=_TIMEOUT, stream=True,
                headers={"User-Agent": "Mozilla/5.0"}
            )

        final = normalize_url(resp.url)
        resolved = final.lower() != normalize_url(url).lower()
        is_aggregator = bool(_AGGREGATOR_DOMAINS.search(final))
        return final, resolved, is_aggregator
    except Exception:
        is_aggregator = bool(_AGGREGATOR_DOMAINS.search(url))
        return normalize_url(url), False, is_aggregator


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
