# envfile.py — the .env loader and the table of every secret (PUB-10)
#
#   python envfile.py          # rewrite .env.example from SECRETS
#
# main.py calls load() first thing on every command, so a local run reads its
# keys from a .env file in the repo root instead of the shell. KEY=VALUE lines,
# "#" comments, optional single or double quotes, an optional leading
# "export ". A variable already set in the environment is never overridden, and
# an empty value is skipped (a copied .env.example sets nothing). The
# workflows have no .env (it is gitignored) and read GitHub Actions secrets.
#
# SECRETS is the one list of what the code reads: doctor's phase-2 messages,
# the generated .env.example and doctor's scrubbing of secret values from
# error text all come from it. tests/test_pub10_setup_plumbing.py checks that
# .env.example matches it.
#
# Standard library only: this runs before anything else is imported.

import os
import re
import sys
from collections import namedtuple

ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(ROOT, ".env")
EXAMPLE_PATH = os.path.join(ROOT, ".env.example")

# name; needed: "required", "optional" or the profile switch it unlocks ("sources.enabled.adzuna",
# "sources.applied_doc"); where: how to get it; purpose: one line.
Secret = namedtuple("Secret", "name needed where purpose")
REQUIRED = "required"

SECRETS = (
    Secret("ANTHROPIC_API_KEY", REQUIRED,
           "console.anthropic.com > API Keys > Create Key",
           "scores each new posting (one Claude call per posting)"),
    Secret("GOOGLE_SERVICE_ACCOUNT_JSON", REQUIRED,
           "Google Cloud console > IAM & Admin > Service Accounts > Keys > Add key (JSON); "
           "paste the whole file on one line, in single quotes",
           "the service account that reads and writes the Sheet"),
    Secret("GOOGLE_SHEET_ID", REQUIRED,
           "the long ID in the Sheet's URL, between /d/ and /edit; share the Sheet with the "
           "service account's client_email as Editor",
           "the Sheet every scored posting is logged to"),
    Secret("GMAIL_ADDRESS", REQUIRED,
           "the Gmail address the digest is sent from",
           "sends the digest; also the inbox the optional readers read"),
    Secret("GMAIL_APP_PASSWORD", REQUIRED,
           "myaccount.google.com/apppasswords (needs 2-Step Verification on the account)",
           "the SMTP and IMAP login for GMAIL_ADDRESS (never the account password)"),
    Secret("ADZUNA_APP_ID", "sources.enabled.adzuna",
           "developer.adzuna.com > Dashboard > API Access Details",
           "the Adzuna job search API (with ADZUNA_API_KEY)"),
    Secret("ADZUNA_API_KEY", "sources.enabled.adzuna",
           "developer.adzuna.com > Dashboard > API Access Details",
           "the Adzuna job search API (with ADZUNA_APP_ID)"),
    Secret("RAPIDAPI_KEY", "sources.enabled.jsearch",
           "rapidapi.com > subscribe to the JSearch API > your app's key",
           "the JSearch job search API"),
    Secret("USAJOBS_API_KEY", "sources.enabled.usajobs",
           "developer.usajobs.gov/apirequest, registered to GMAIL_ADDRESS",
           "federal postings from the USAJOBS API"),
    Secret("GOOGLE_DOC_ID", "sources.applied_doc",
           "the ID in your job-index Google Doc's URL, between /d/ and /edit; share the Doc "
           "with the service account (Viewer is enough)",
           "seeds Applied rows in the Sheet from the jobs listed in that Doc"),
    Secret("RECIPIENT_EMAIL", "optional",
           "any address",
           "sends the weekly summary here instead of delivery.email_to"),
)

NAMES = tuple(s.name for s in SECRETS)
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def secret(name):
    return next(s for s in SECRETS if s.name == name)


def _value(raw):
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # An unquoted value may end in a " # comment".
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def parse(text, path=".env"):
    """[(key, value)] from .env text. A line that is not a comment, a blank
    or KEY=VALUE stops the run with its line number (never its text, which
    may hold a secret)."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.fullmatch(key):
            raise SystemExit(f"{path} line {n}: not a KEY=VALUE line (comments start with #). Fix it and run again.")
        out.append((key, _value(value)))
    return out


def load(path=ENV_PATH, environ=None):
    """Set each KEY=VALUE from `path` that the environment does not already
    have. Returns the names set (never the values). No file: nothing set."""
    environ = os.environ if environ is None else environ
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        pairs = parse(fh.read(), os.path.relpath(path, ROOT))
    loaded = []
    for key, value in pairs:
        if key in environ or value == "":
            continue
        environ[key] = value
        loaded.append(key)
    return loaded


def example_text():
    """The .env.example file, generated from SECRETS."""
    lines = [
        "# .env.example — copy to .env in the repo root and fill in what you use.",
        "# .env is gitignored: never commit it. Generated from envfile.SECRETS by",
        "# `python envfile.py`; edit the table, not this file.",
        "#",
        "# main.py loads .env on every command; a variable already set in your shell",
        "# wins, and an empty value counts as unset. The workflows read GitHub",
        "# Actions secrets with the same names instead.",
    ]
    groups = (("Required", [s for s in SECRETS if s.needed == REQUIRED]),
              ("Optional: each unlocks one source or switch", [s for s in SECRETS if s.needed.startswith("sources.")]),
              ("Optional", [s for s in SECRETS if s.needed == "optional"]))
    for title, items in groups:
        lines += ["", f"# --- {title} ---"]
        for s in items:
            lines.append("")
            purpose = s.purpose if s.needed.startswith("sources.") else s.purpose[0].upper() + s.purpose[1:]
            unlocks = f"Unlocks {s.needed}: " if s.needed.startswith("sources.") else ""
            lines.append(f"# {unlocks}{purpose}.")
            lines.append(f"# Get it: {s.where}.")
            lines.append(f"{s.name}=")
    return "\n".join(lines) + "\n"


def write_example(path=EXAMPLE_PATH):
    with open(path, "w") as fh:
        fh.write(example_text())
    return path


if __name__ == "__main__":
    print(f"wrote {os.path.relpath(write_example(), ROOT)}")
    sys.exit(0)
