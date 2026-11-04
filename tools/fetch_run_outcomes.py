#!/usr/bin/env python3
"""Outcome of every fetch.yml run since the previous digest (OPS-12).

Run by daily.yml just before `python main.py digest`. Writes a JSON list, one
object per completed fetch run in the window:

    {"run_id", "started_at", "conclusion", "url", "error_title", "error_message"}

error_title / error_message come from the run's first failure annotation (a
`::error title=...::` line, e.g. OPS-12's "Anthropic API access failed"), and
are only looked up for runs that did not succeed. main.py reads the file
(FETCH_RUNS_FILE) and the digest shows a red banner per failed or cancelled run.

The window opens when the previous successful digest (daily.yml) started, so
each fetch run is reported by one digest. A fetch run counts by when it
finished: one still running when the last digest started is reported by this one.
With no earlier digest on record, the window is the last 48 hours.

Never blocks the digest: daily.yml runs this with continue-on-error, and
main.py sends without a banner when the file is missing.

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo python tools/fetch_run_outcomes.py out.json
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

API = "https://api.github.com"
FETCH_WORKFLOW = "fetch.yml"
DIGEST_WORKFLOW = "daily.yml"
FALLBACK_WINDOW = timedelta(hours=48)
PER_PAGE = 100   # fetch.yml runs three times a day; 100 covers a month

Get = Callable[[str, dict], Any]


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_start(get: Get, repo: str, current_run_id: str | None, now: datetime) -> datetime:
    """When the previous successful digest started, or now - 48h if there is none."""
    runs = get(f"{API}/repos/{repo}/actions/workflows/{DIGEST_WORKFLOW}/runs",
               {"status": "success", "per_page": 10}).get("workflow_runs", [])
    for run in runs:   # newest first
        if str(run["id"]) == str(current_run_id or ""):
            continue
        return _ts(run.get("run_started_at") or run["created_at"])
    return now - FALLBACK_WINDOW


def first_error(get: Get, repo: str, run_id: int) -> tuple[str, str]:
    """(title, message) of the run's first failure annotation, preferring one with a title."""
    jobs = get(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 20}).get("jobs", [])
    untitled = ("", "")
    for job in jobs:
        notes = get(f"{API}/repos/{repo}/check-runs/{job['id']}/annotations", {"per_page": 50})
        for note in notes or []:
            if note.get("annotation_level") != "failure":
                continue
            title = (note.get("title") or "").strip()
            message = (note.get("message") or "").strip()
            if title:
                return title, message
            if not untitled[1]:
                untitled = ("", message)
    return untitled


def collect(get: Get, repo: str, current_run_id: str | None = None,
            now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    since = window_start(get, repo, current_run_id, now)
    runs = get(f"{API}/repos/{repo}/actions/workflows/{FETCH_WORKFLOW}/runs",
               {"status": "completed", "per_page": PER_PAGE}).get("workflow_runs", [])
    out = []
    for run in runs:
        if _ts(run["updated_at"]) <= since:
            continue
        conclusion = run.get("conclusion") or ""
        title = message = ""
        if conclusion != "success":
            title, message = first_error(get, repo, run["id"])
        out.append({
            "run_id": run["id"],
            "started_at": run.get("run_started_at") or run["created_at"],
            "conclusion": conclusion,
            "url": run["html_url"],
            "error_title": title,
            "error_message": message,
        })
    out.sort(key=lambda r: r["started_at"])
    print(f"[FetchRuns] {len(out)} fetch runs since {since.isoformat(timespec='minutes')}: "
          + (", ".join(f"{r['run_id']} {r['conclusion']}" for r in out) or "none"))
    return out


def _github_get(token: str) -> Get:
    import requests

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"})

    def get(url: str, params: dict) -> Any:
        response = session.get(url, params=params, timeout=30)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {response.status_code} for {url}: {response.text[:300]}")
        return response.json()
    return get


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python tools/fetch_run_outcomes.py <out.json>")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        sys.exit("GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
    runs = collect(_github_get(token), repo, os.environ.get("GITHUB_RUN_ID"))
    with open(sys.argv[1], "w") as f:
        json.dump(runs, f, indent=2)


if __name__ == "__main__":
    main()
