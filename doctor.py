# doctor.py — `python main.py doctor`: is this install ready to run? (PUB-06)
#
#   python main.py doctor              # all three phases, plus the state files and Sheet retry files
#   python main.py doctor --phase 1    # Python, dependencies, repo files, git remote, repository visibility
#   python main.py doctor --phase 2    # one line per credential (makes one small call each)
#   python main.py doctor --phase 3    # profile, dream companies, pinned boards, schedule
#   python main.py doctor --json       # the same results as a JSON list
#
# Read-only: it never writes a file, a Sheet cell or an email, and never
# prints a secret's value -- only the variable's name and "set" / "missing".
# Phase 2's calls are the smallest each service offers: a login, opening the
# Sheet, one one-token Claude call on the cheapest model, one search per job
# API. A credential a disabled source or reader would use is SKIP, not FAIL,
# and the SKIP names the switch that would turn it on. With no profile.yaml
# yet, every optional source counts as on (phase 2 says so at the top).
#
# WARN (PUB-10) is a problem that does not stop a run: the git remote needs
# credentials this machine does not have.
#
# The last line is always "DOCTOR phase N: X pass, Y fail, Z skip, W warn" (or
# "DOCTOR all: ..."); the exit code is 1 only when something FAILed.

import argparse
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys

import envfile

ROOT = os.path.dirname(os.path.abspath(__file__))

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
STATUSES = (PASS, FAIL, SKIP, WARN)
NO_PROFILE_NOTE = "no profile yet; optional sources show FAIL until phase 3 marks them disabled"

# Every secret the code reads (envfile.SECRETS, PUB-10). Their values are
# scrubbed out of any error text before it is printed (a requests error can
# carry Adzuna's key in its URL).
SECRET_VARS = envfile.NAMES

# requirements.txt name -> the module it installs.
IMPORT_NAMES = {"anthropic": "anthropic", "requests": "requests", "beautifulsoup4": "bs4",
                "gspread": "gspread", "google-auth": "google.auth", "PyYAML": "yaml",
                "google-api-python-client": "googleapiclient"}

REPO_FILES = ("profile.example.yaml", "prompts/scoring_system.txt", "prompts/weekly_declines.txt",
              "data/us_places.tsv.gz")

STATE_FILES = ("seen_jobs.json", "skip_state.json", "bespoke_state.json", "bespoke_cursor.json",
               "shard_state.json", "status_email_cache.json", "company_profiles.json", "fetch_budget.json")

# The cheapest Claude model, for the one-token key check.
CHEAPEST_MODEL = "claude-haiku-4-5"
TIMEOUT = 15


def result(phase, name, status, reason):
    return {"phase": phase, "check": name, "status": status, "reason": reason}


def _scrub(text):
    text = " ".join(str(text).split())
    for var in SECRET_VARS:
        value = os.environ.get(var, "").strip()
        if len(value) >= 4:
            text = text.replace(value, f"<{var}>")
    return text[:200]


def _error(e):
    return _scrub(f"{type(e).__name__}: {e}")


def _env(*names):
    """(values, missing names). Values are never printed."""
    values = {n: os.environ.get(n, "").strip() for n in names}
    return values, [n for n, v in values.items() if not v]


def _missing(phase, name, missing):
    """FAIL naming each missing variable and where to get it (envfile.SECRETS)."""
    where = "; ".join(f"{n}: {envfile.secret(n).where}" for n in missing)
    return result(phase, name, FAIL, f"{', '.join(missing)} missing: set {'it' if len(missing) == 1 else 'them'} "
                                     f"in .env (local; see .env.example) or as GitHub Actions secrets (workflows) — {where}.")


# --- Phase 1: the install ---------------------------------------------------------

def required_python(path=None):
    """(major, minor) from requirements.txt's "# python>=3.11" line."""
    with open(path or os.path.join(ROOT, "requirements.txt")) as fh:
        m = re.search(r"#\s*python\s*>=\s*(\d+)\.(\d+)", fh.read())
    return (int(m.group(1)), int(m.group(2))) if m else (3, 11)


def _requirements(path=None):
    with open(path or os.path.join(ROOT, "requirements.txt")) as fh:
        lines = [l.split("#")[0].strip() for l in fh]
    return [re.split(r"[=<>!~ ]", l)[0] for l in lines if l]


def _git_remote():
    """(status, reason) for `git ls-remote origin`; offline is SKIP."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                             text=True, timeout=TIMEOUT)
        if url.returncode != 0:
            return FAIL, "this folder has no git remote named origin: clone the repository with git rather than downloading a zip."
        # No prompt: a private remote with no stored credentials fails at once.
        out = subprocess.run(["git", "ls-remote", "--exit-code", "origin", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=TIMEOUT,
                             env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except FileNotFoundError:
        return FAIL, "git is not installed: install it (https://git-scm.com) so the workflows' state commits can be pulled."
    except subprocess.TimeoutExpired:
        return SKIP, "git remote did not answer within 15 s (offline?); check again when connected."
    if out.returncode == 0:
        return PASS, f"origin {_scrub(url.stdout.strip())} answers."
    err = out.stderr.lower()
    if "could not resolve host" in err or "network is unreachable" in err or "timed out" in err:
        return SKIP, "offline: origin could not be reached; check again when connected."
    if "username" in err or "authentication" in err or "403" in err:
        return WARN, (f"origin {_scrub(url.stdout.strip())} needs credentials git does not have: "
                      f"run gh auth login (or set up a git credential helper) before pulling the workflows' commits.")
    return FAIL, f"origin {_scrub(url.stdout.strip())} refused git ls-remote: check the URL and your git credentials ({_scrub(out.stderr)[:80]})."


# REV-01: the same words the workflows' guard prints (without its "Nothing was written.").
PUBLIC_REPO_MESSAGE = ("This repository is public or a fork. The agent commits your profile and job history to it; "
                       "make the repository private (Settings > General > Danger Zone) or use 'Use this template' "
                       "instead of Fork.")


def _allow_public_repo():
    """delivery.allow_public_repo from the profile; false when there is no
    valid profile yet (phase 3 reports why)."""
    profile = _profile_or_none()
    return bool(profile) and (profile.get("delivery") or {}).get("allow_public_repo") is True


def _repo_visibility(run=subprocess.run, allow=None):
    """(status, reason) for `gh repo view --json isPrivate,isFork` (REV-01):
    PASS private and not a fork; FAIL public or a fork, WARN instead when
    delivery.allow_public_repo is true; WARN when gh cannot tell."""
    try:
        out = run(["gh", "repo", "view", "--json", "isPrivate,isFork"], cwd=ROOT, capture_output=True,
                  text=True, timeout=TIMEOUT, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
    except FileNotFoundError:
        return WARN, ("gh (the GitHub CLI) is not installed, so the repository's visibility was not checked: "
                      "install it (https://cli.github.com) or confirm on GitHub that the repository is private. "
                      "The workflows refuse to run in a public repository.")
    except subprocess.TimeoutExpired:
        return WARN, "gh repo view did not answer within 15 s (offline?); visibility not checked."
    if out.returncode != 0:
        return WARN, (f"gh could not read the repository ({_scrub(out.stderr.strip())[:120] or 'no output'}); "
                      f"run gh auth login, or confirm on GitHub that the repository is private.")
    try:
        info = json.loads(out.stdout)
        private, fork = info["isPrivate"], info["isFork"]
    except (ValueError, KeyError, TypeError):
        return WARN, f"gh repo view gave an answer doctor cannot read ({_scrub(out.stdout)[:80]}); visibility not checked."
    if private is True and fork is not True:
        return PASS, "private, not a fork."
    allow = _allow_public_repo() if allow is None else allow
    what = "public" if private is not True else "a fork"
    if allow:
        return WARN, (f"the repository is {what} and delivery.allow_public_repo is true: everything the agent "
                      f"commits (your profile and job history) is readable by anyone.")
    return FAIL, PUBLIC_REPO_MESSAGE


def phase1():
    out = []
    need = required_python()
    have = sys.version_info[:2]
    out.append(result(1, "python", PASS if have >= need else FAIL,
                      f"Python {have[0]}.{have[1]} (needs {need[0]}.{need[1]} or later)." if have >= need else
                      f"Python {have[0]}.{have[1]} is too old: install {need[0]}.{need[1]} or later and recreate the virtualenv."))
    for name in _requirements():
        module = IMPORT_NAMES.get(name, name.replace("-", "_"))
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        out.append(result(1, f"dependency {name}", PASS if found else FAIL,
                          f"imports as {module}." if found else
                          f"not importable as {module}: run pip install -r requirements.txt."))
    for rel in REPO_FILES:
        ok = os.path.exists(os.path.join(ROOT, rel))
        out.append(result(1, f"file {rel}", PASS if ok else FAIL,
                          "present." if ok else "missing from the checkout: pull the repository again."))
    status, reason = _git_remote()
    out.append(result(1, "git remote", status, reason))
    status, reason = _repo_visibility()
    out.append(result(1, "repository visibility", status, reason))
    return out


# --- Phase 2: credentials ------------------------------------------------------------

def _profile_or_none():
    try:
        import profile_loader
        return profile_loader.active()
    except Exception:                                  # noqa: BLE001 -- phase 3 reports why
        return None


def _enabled(profile, key):
    """sources.enabled.<key>; with no profile yet, every source counts as on."""
    return True if profile is None else bool(profile["sources"]["enabled"].get(key))


def _reader(profile, key):
    """sources.<key>.enabled for the inbox readers (PUB-10); on with no profile."""
    return True if profile is None else bool(profile["sources"][key]["enabled"])


def _off(name, switch, use):
    return result(2, name, SKIP, f"{switch} is false: set it true to {use}; this check then runs.")


def check_gmail_imap(profile):
    if not (_enabled(profile, "linkedin_email") or _reader(profile, "status_tracking")):
        return result(2, "GMAIL imap", SKIP, "nothing reads the inbox: sources.enabled.linkedin_email and "
                                             "sources.status_tracking.enabled are both false; set either true "
                                             "and this check runs.")
    values, missing = _env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    if missing:
        return _missing(2, "GMAIL imap", missing)
    import imaplib
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=TIMEOUT)
        mail.login(values["GMAIL_ADDRESS"], values["GMAIL_APP_PASSWORD"])
    except Exception as e:                             # noqa: BLE001
        return result(2, "GMAIL imap", FAIL, f"IMAP login refused ({_error(e)}): use a Gmail app password "
                                             f"(myaccount.google.com/apppasswords), not the account password, and turn IMAP on in Gmail settings.")
    try:
        if profile is None:
            return result(2, "GMAIL imap", PASS, "IMAP login OK (no profile yet: the LinkedIn label is checked "
                                                 "once phase 3 names it).")
        if not _enabled(profile, "linkedin_email"):
            return result(2, "GMAIL imap", PASS, "IMAP login OK (linkedin_email is disabled, so no label to check).")
        label = profile["sources"]["linkedin_label"]
        status, _ = mail.select(f'"{label}"', readonly=True)
        if status != "OK":
            return result(2, "GMAIL imap", FAIL, f"IMAP login OK, but the Gmail label {label!r} "
                                                 f"(sources.linkedin_label) does not exist: create it and a filter that files LinkedIn job alerts under it.")
        return result(2, "GMAIL imap", PASS, f"IMAP login OK; label {label!r} found.")
    finally:
        try:
            mail.logout()
        except Exception:                              # noqa: BLE001
            pass


def check_gmail_smtp(profile):
    values, missing = _env("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
    if missing:
        return _missing(2, "GMAIL smtp", missing)
    import smtplib
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as server:
            server.login(values["GMAIL_ADDRESS"], values["GMAIL_APP_PASSWORD"])
    except Exception as e:                             # noqa: BLE001
        return result(2, "GMAIL smtp", FAIL, f"SMTP login refused ({_error(e)}): use a Gmail app password, "
                                             f"not the account password.")
    return result(2, "GMAIL smtp", PASS, "SMTP login OK (nothing sent).")


def check_google(profile):
    values, missing = _env("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID")
    if missing:
        return _missing(2, "GOOGLE sheet", missing)
    try:
        info = json.loads(values["GOOGLE_SERVICE_ACCOUNT_JSON"])
        email = info["client_email"]
    except (ValueError, KeyError, TypeError) as e:
        return result(2, "GOOGLE sheet", FAIL, f"GOOGLE_SERVICE_ACCOUNT_JSON does not parse as a service account "
                                               f"key ({type(e).__name__}): paste the whole downloaded .json file's contents.")
    import sheet_layout                                # PUB-07b: no profile needed (config loads one)
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        book = gspread.authorize(creds).open_by_key(values["GOOGLE_SHEET_ID"])
    except Exception as e:                             # noqa: BLE001
        return result(2, "GOOGLE sheet", FAIL, f"the Sheet would not open ({_error(e)}): check GOOGLE_SHEET_ID and "
                                               f"share the Sheet with {email} as Editor.")
    titles = [ws.title for ws in book.worksheets()]
    missing_tabs = [t for t in (sheet_layout.SHEET_NAME, sheet_layout.DISQUALIFIED_SHEET_NAME) if t not in titles]
    if missing_tabs:
        return result(2, "GOOGLE sheet", FAIL, f"the Sheet opened but has no tab named {', '.join(map(repr, missing_tabs))}: "
                                               f"run python main.py sheet-init on a blank Sheet, or rename the tabs.")
    header = book.worksheet(sheet_layout.SHEET_NAME).row_values(1)
    role = _drive_role(creds, values["GOOGLE_SHEET_ID"], email)
    access = (f"the service account is {role}" if role else
              "read OK, write not verified (enabling the Google Drive API in the same Cloud project lets this "
              "check read the service account's role)")
    out = [result(2, "GOOGLE sheet", PASS, f"Sheet opened; both tabs found; header row has {len(header)} columns; {access}.")]
    for title in (sheet_layout.SHEET_NAME, sheet_layout.DISQUALIFIED_SHEET_NAME):
        row1 = header if title == sheet_layout.SHEET_NAME else book.worksheet(title).row_values(1)
        out.append(header_check(title, row1))
    return out


def header_check(title, row1):
    """REV-04: one tab's row 1 against its required headers (any order) and
    the layout's optional ones."""
    import sheet_layout
    name = f"GOOGLE headers {title!r}"
    if not any(str(c).strip() for c in row1):
        return result(2, name, WARN, "row 1 is empty: the first write lays out the header row "
                                     "(or run python main.py sheet-init).")
    missing, repeated = sheet_layout.header_problems(title, row1)
    required = sheet_layout.REQUIRED_HEADERS[title]
    if missing or repeated:
        parts = ([f"required header missing: {', '.join(missing)}"] if missing else []) + \
                ([f"header repeated: {', '.join(repeated)}"] if repeated else [])
        return result(2, name, FAIL, f"{'; '.join(parts)}. Every write to this tab stops until row 1 is "
                                     f"fixed (any order; exact text).")
    optional = sheet_layout.optional_missing(title, row1)
    if optional:
        return result(2, name, WARN, f"all {len(required)} required present; "
                                     + "; ".join(f"optional column missing: {o}" for o in optional)
                                     + " (written only if you add the header; never created).")
    return result(2, name, PASS, f"all {len(required)} required present; no optional column missing.")


def _drive_role(creds, file_id, email):
    """The service account's role on the file, or None if the Drive API will not say."""
    try:
        from googleapiclient.discovery import build
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        perms = drive.permissions().list(fileId=file_id, fields="permissions(emailAddress,role)",
                                         supportsAllDrives=True).execute()
        for p in perms.get("permissions", []):
            if p.get("emailAddress", "").lower() == email.lower():
                return p.get("role")
    except Exception:                                  # noqa: BLE001
        return None
    return None


def check_google_doc(profile):
    if not _reader(profile, "applied_doc"):
        return _off("GOOGLE doc", "sources.applied_doc.enabled", "seed Applied rows from your job-index Google Doc "
                                                                 "(GOOGLE_DOC_ID)")
    values, missing = _env("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_DOC_ID")
    if missing:
        return _missing(2, "GOOGLE doc", missing)
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_service_account_info(json.loads(values["GOOGLE_SERVICE_ACCOUNT_JSON"]),
                                                      scopes=["https://www.googleapis.com/auth/documents.readonly"])
        doc = build("docs", "v1", credentials=creds, cache_discovery=False).documents().get(
            documentId=values["GOOGLE_DOC_ID"], fields="title").execute()
    except Exception as e:                             # noqa: BLE001
        return result(2, "GOOGLE doc", FAIL, f"the Doc would not open ({_error(e)}): check GOOGLE_DOC_ID and share "
                                             f"the Doc with the service account (Viewer is enough).")
    return result(2, "GOOGLE doc", PASS, f"Doc opened ({len(doc.get('title', ''))}-character title).")


def check_anthropic(profile):
    values, missing = _env("ANTHROPIC_API_KEY")
    if missing:
        return _missing(2, "ANTHROPIC", missing)
    import llm
    try:
        llm.complete(CHEAPEST_MODEL, None, [{"role": "user", "content": "ok"}], 1)
    except llm.ApiAccessError as e:
        return result(2, "ANTHROPIC", FAIL, f"one-token call on {CHEAPEST_MODEL} refused ({e.cause}): check the key "
                                            f"and the account's credit balance.")
    except Exception as e:                             # noqa: BLE001
        return result(2, "ANTHROPIC", FAIL, f"one-token call on {CHEAPEST_MODEL} failed ({_error(e)}): check the key "
                                            f"and the account's credit balance.")
    return result(2, "ANTHROPIC", PASS, f"one-token call on {CHEAPEST_MODEL} answered.")


def _http(phase, name, url, what, **kw):
    import requests
    try:
        resp = requests.get(url, timeout=TIMEOUT, **kw)
    except Exception as e:                             # noqa: BLE001
        return result(phase, name, FAIL, f"no answer from {url.split('/')[2]} ({_error(e)}); check the network.")
    if resp.status_code in (401, 403):
        return result(phase, name, FAIL, f"HTTP {resp.status_code}: the key was refused — {what}.")
    if resp.status_code == 429:
        return result(phase, name, FAIL, "HTTP 429: the key works but its quota is used up for now; check the plan.")
    if not resp.ok:
        return result(phase, name, FAIL, f"HTTP {resp.status_code} from {url.split('/')[2]}; try again later.")
    return result(phase, name, PASS, "one search answered.")


def check_adzuna(profile):
    if not _enabled(profile, "adzuna"):
        return _off("ADZUNA", "sources.enabled.adzuna", "fetch Adzuna postings")
    values, missing = _env("ADZUNA_APP_ID", "ADZUNA_API_KEY")
    if missing:
        return _missing(2, "ADZUNA", missing)
    return _http(2, "ADZUNA", "https://api.adzuna.com/v1/api/jobs/us/search/1",
                 "check ADZUNA_APP_ID and ADZUNA_API_KEY at developer.adzuna.com",
                 params={"app_id": values["ADZUNA_APP_ID"], "app_key": values["ADZUNA_API_KEY"],
                         "what": "IT director", "results_per_page": 1})


def check_rapidapi(profile):
    if not _enabled(profile, "jsearch"):
        return _off("RAPIDAPI", "sources.enabled.jsearch", "fetch JSearch postings")
    values, missing = _env("RAPIDAPI_KEY")
    if missing:
        return _missing(2, "RAPIDAPI", missing)
    return _http(2, "RAPIDAPI", "https://jsearch.p.rapidapi.com/search",
                 "check RAPIDAPI_KEY and that it is subscribed to JSearch on rapidapi.com",
                 headers={"X-RapidAPI-Key": values["RAPIDAPI_KEY"], "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
                 params={"query": "IT director United States", "page": "1", "num_pages": "1"})


def check_usajobs(profile):
    if not _enabled(profile, "usajobs"):
        return _off("USAJOBS", "sources.enabled.usajobs", "fetch USAJOBS federal postings")
    values, missing = _env("USAJOBS_API_KEY", "GMAIL_ADDRESS")
    if missing:
        return _missing(2, "USAJOBS", missing)
    return _http(2, "USAJOBS", "https://data.usajobs.gov/api/search",
                 "the key must be registered at developer.usajobs.gov to the GMAIL_ADDRESS email",
                 headers={"Host": "data.usajobs.gov", "User-Agent": values["GMAIL_ADDRESS"],
                          "Authorization-Key": values["USAJOBS_API_KEY"]},
                 params={"Keyword": "CIO", "ResultsPerPage": 1})


PHASE2 = (check_gmail_imap, check_gmail_smtp, check_google, check_google_doc, check_anthropic,
          check_adzuna, check_rapidapi, check_usajobs)


def phase2():
    profile = _profile_or_none()
    out = []
    for check in PHASE2:
        try:
            got = check(profile)
            out.extend(got if isinstance(got, list) else [got])   # REV-04: the Sheet check adds header rows
        except Exception as e:                         # noqa: BLE001 -- one broken check never hides the rest
            out.append(result(2, check.__name__.removeprefix("check_").upper(), FAIL, f"check crashed: {_error(e)}"))
    return out


# --- Phase 3: configuration ----------------------------------------------------------

def phase3():
    out = []
    import profile_loader
    if not os.path.exists(profile_loader.DEFAULT_PATH):
        return [result(3, "profile", FAIL, "profile.yaml not found: run python main.py setup, or copy "
                                           "profile.example.yaml to profile.yaml and edit it.")]
    try:
        data = profile_loader.read()
        errors = profile_loader.validate(data)
    except FileNotFoundError:
        return [result(3, "profile", FAIL, "profile.yaml not found: run python main.py setup, or copy "
                                           "profile.example.yaml to profile.yaml and edit it.")]
    except Exception as e:                             # noqa: BLE001
        return [result(3, "profile", FAIL, f"profile.yaml does not load ({_error(e)}): fix the YAML syntax.")]
    if errors:
        out.append(result(3, "profile", FAIL, f"{len(errors)} problem(s), first: {errors[0]} "
                                              f"(python main.py profile-check lists them all)."))
        return out
    out.append(result(3, "profile", PASS, "profile.yaml loads and validates."))
    profile = profile_loader.active()

    path = profile["dream_companies"]["file"]
    try:
        names = profile_loader.dream_companies(profile)
        out.append(result(3, "dream companies", PASS if names else FAIL,
                          f"{path}: {len(names)} companies." if names else
                          f"{path} is empty: add one company name per line (or run setup)."))
    except FileNotFoundError:
        out.append(result(3, "dream companies", FAIL, f"{path} (dream_companies.file) does not exist: create it, "
                                                      f"one company name per line."))

    out.append(_check_pinned())

    import config
    import schedule
    delivery = profile["delivery"]
    stale = schedule.mismatches(delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES)
    out.append(result(3, "workflow schedule", FAIL if stale else PASS,
                      f"{', '.join(stale)} out of date: run python main.py cron --write and commit the workflows."
                      if stale else "daily.yml and weekly_summary.yml match python main.py cron."))

    lead = config.CRON_LEAD_MINUTES
    slot = schedule.slots(delivery, lead)["digest"]
    fire = f"{slot.minutes // 60:02d}:{slot.minutes % 60:02d}"
    sane = 0 < lead < 24 * 60
    out.append(result(3, "digest timing", PASS if sane else FAIL,
                      f"the digest cron fires at {fire} {delivery['timezone']}, {lead} min before "
                      f"{delivery['digest_time_local']}." if sane else
                      f"config.CRON_LEAD_MINUTES {lead} is not between 1 and 1439 minutes."))
    return out


def _check_pinned():
    import yaml
    path = os.path.join(ROOT, "data", "companies.yaml")
    try:
        with open(path) as fh:
            pinned = (yaml.safe_load(fh) or {}).get("pinned") or {}
    except FileNotFoundError:
        return result(3, "pinned boards", PASS, "data/companies.yaml absent (no pinned boards).")
    except yaml.YAMLError as e:
        return result(3, "pinned boards", FAIL, f"data/companies.yaml is not valid YAML ({_error(e)}).")
    bad = []
    for name, entry in (pinned.get("workday") or {}).items():
        if not (isinstance(entry, dict) and all(entry.get(k) for k in ("host", "tenant", "site"))):
            bad.append(f"workday {name} (needs host, tenant, site)")
    for name, url in (pinned.get("careers_pages") or {}).items():
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            bad.append(f"careers_pages {name} (needs an http(s) URL)")
    unknown = set(pinned) - {"workday", "careers_pages"}
    bad += [f"unknown section {k!r}" for k in sorted(unknown)]
    count = len(pinned.get("workday") or {}) + len(pinned.get("careers_pages") or {})
    if bad:
        return result(3, "pinned boards", FAIL, f"data/companies.yaml pinned: {'; '.join(bad[:3])}.")
    return result(3, "pinned boards", PASS, f"{count} pinned: entries in data/companies.yaml parse.")


# --- State files (no --phase) ---------------------------------------------------------

def state_files():
    out = []
    paths = [os.path.join(ROOT, f) for f in STATE_FILES]
    paths += sorted(glob.glob(os.path.join(ROOT, "state", "pending", "*.json")))
    present = 0
    for path in paths:
        rel = os.path.relpath(path, ROOT)
        if not os.path.exists(path):
            continue
        present += 1
        try:
            with open(path) as fh:
                json.load(fh)
        except (ValueError, OSError) as e:
            out.append(result(0, f"state {rel}", FAIL, f"does not parse ({type(e).__name__}): restore it from git "
                                                     f"history (git log -- {rel})."))
    if not out:
        out.append(result(0, "state files", PASS, f"{present} present, all parse (absent is fine on a fresh clone)."))
    return out


# --- Sheet retry files (no --phase; REV-03) -------------------------------------------

def sheet_retry_files():
    """Rows a digest could not write to the Sheet, waiting in state/sheet_retry/."""
    import sheet_retry
    count, oldest, bad = sheet_retry.summary()
    out = []
    if bad:
        out.append(result(0, "sheet retry", FAIL,
                          f"{len(bad)} unreadable retry file(s) renamed .bad ({', '.join(bad[:3])}): their "
                          f"postings are not on the Sheet; fix the JSON and rename back to .json."))
    if count:
        rows = len(sheet_retry.waiting_rows())
        out.append(result(0, "sheet retry", WARN,
                          f"{count} file(s) waiting ({rows} postings), oldest {oldest}: the next digest "
                          f"retries them; the digest log says why the last write failed."))
    if not out:
        out.append(result(0, "sheet retry", PASS, "no Sheet rows waiting in state/sheet_retry/."))
    return out


# --- Scoring cost (no --phase; PUB-12) ------------------------------------------------

def scoring_cost():
    """The scoring model, the per-run call cap and, when config.MODEL_PRICES
    knows the model, the estimated cost of one call (config.SCORING_TOKENS_PER_CALL,
    measured by calibrate) and of a run at the cap."""
    import config
    import profile_loader
    try:
        scoring = profile_loader.active()["scoring"]
    except Exception:                                  # noqa: BLE001 -- phase 3 reports why
        return []
    model, cap = scoring["model"], scoring["max_calls_per_run"]
    per_call, how = scoring_call_estimate(model)
    if per_call is None:
        return [result(0, "scoring cost", WARN, f"model {model}, at most {cap} scoring calls a fetch run; cost "
                                                f"unknown ({model} is not in config.MODEL_PRICES).")]
    day = scoring.get("max_calls_per_day")
    return [result(0, "scoring cost", PASS, f"model {model}, at most {cap} scoring calls a fetch run; about "
                                            f"{config.format_cost(per_call)} a call ({how}), so at most "
                                            f"{config.format_cost(per_call * cap)} a run"
                                            + (f" and {config.format_cost(per_call * day)} a day" if day else "")
                                            + " (estimate; calibrate measures it).")]


# REV-06/07: the uncached share of a scoring call's input with the prompt
# cached: 3,584 of 24,257 input tokens in the 9 Nov calibrate run (15%).
DEFAULT_UNCACHED_SHARE = 0.15


def scoring_call_estimate(model, usage_file=None):
    """(US$ a scoring call, how it was estimated). With a cache price for the
    model, the input is split by the last calibrate run's uncached share
    (out/calibrate_usage.json) or DEFAULT_UNCACHED_SHARE, and the cached part
    priced as cache reads; without one, all input at the input price."""
    import config
    tokens_in, tokens_out = config.SCORING_TOKENS_PER_CALL
    if model not in config.MODEL_CACHE_PRICES:
        return config.model_cost(model, tokens_in, tokens_out), "uncached"
    share, source = DEFAULT_UNCACHED_SHARE, "assumed 15% uncached input"
    try:
        import calibrate
        with open(usage_file or calibrate.USAGE_FILE) as fh:
            last = json.load(fh)
        total = (last.get("uncached") or 0) + (last.get("cache_write") or 0) + (last.get("cache_read") or 0)
        if total and last.get("model") == model:
            share = (last.get("uncached") or 0) / total
            source = f"{share:.0%} uncached input in the last calibrate run"
    except (OSError, ValueError, ImportError):
        pass
    uncached = round(tokens_in * share)
    return config.model_cost(model, uncached, tokens_out, 0, tokens_in - uncached), f"prompt cached, {source}"


# --- LinkedIn pages (no --phase; REV-07) -------------------------------------------

def linkedin_pages():
    """WARN when LinkedIn failed more than half its job pages over the last
    three fetch runs that tried any (fetch_budget.json, committed by fetch)."""
    import fetch_budget
    try:
        failed, tried, runs = fetch_budget.linkedin_blocked_share(runs=3)
    except (OSError, ValueError) as e:
        return [result(0, "linkedin pages", WARN, f"fetch_budget.json does not parse ({type(e).__name__}).")]
    if not runs:
        return [result(0, "linkedin pages", PASS, "no fetch run has tried a LinkedIn job page yet.")]
    if failed * 2 > tried:
        return [result(0, "linkedin pages", WARN,
                       f"LinkedIn blocked or hid {failed} of {tried} job pages over the last {runs} fetch "
                       f"run(s). LinkedIn blocks GitHub runner IPs: the alert-label reader still works, the "
                       f"page reader may not, so those jobs are retried and then recorded as \"LinkedIn page "
                       f"unavailable\".")]
    return [result(0, "linkedin pages", PASS, f"{tried - failed} of {tried} job pages read over the last "
                                              f"{runs} fetch run(s).")]


# --- Output ----------------------------------------------------------------------------

def run(phase=None):
    if phase == 1:
        return phase1()
    if phase == 2:
        return phase2()
    if phase == 3:
        return phase3()
    return phase1() + phase2() + phase3() + state_files() + sheet_retry_files() + linkedin_pages() + scoring_cost()


def summary(results, phase=None):
    counts = {s: sum(1 for r in results if r["status"] == s) for s in STATUSES}
    return (f"DOCTOR {'phase ' + str(phase) if phase else 'all'}: "
            f"{counts[PASS]} pass, {counts[FAIL]} fail, {counts[SKIP]} skip, {counts[WARN]} warn")


def notes(phase=None):
    """Lines printed above the results."""
    if phase in (None, 2) and not os.path.exists(_profile_path()):
        return [NO_PROFILE_NOTE]
    return []


def _profile_path():
    import profile_loader
    return profile_loader.DEFAULT_PATH


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python main.py doctor", description="Read-only install check (PUB-06)")
    parser.add_argument("--phase", type=int, choices=(1, 2, 3))
    parser.add_argument("--json", action="store_true", help="print the results as a JSON list")
    args = parser.parse_args(argv)
    results = run(args.phase)
    if args.json:
        print(json.dumps([{"note": n} for n in notes(args.phase)] + results +
                         [{"summary": summary(results, args.phase)}], indent=2))
    else:
        pending_notes = notes(args.phase)
        width = max(len(r["check"]) for r in results)
        for r in results:
            if pending_notes and r["phase"] == 2:
                # At the top of phase 2, before its first line.
                for note in pending_notes:
                    print(f"[phase 2] NOTE  {note}")
                pending_notes = []
            label = (f"phase {r['phase']}" if r["phase"] else
                     "cost   " if r["check"] == "scoring cost" else "state  ")
            print(f"[{label}] {r['status']}  {r['check']:<{width}}  {r['reason']}")
        print(summary(results, args.phase))
    return 1 if any(r["status"] == FAIL for r in results) else 0
