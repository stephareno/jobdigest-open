# fetch_budget.py — the daily ceiling on model calls in fetch runs (REV-06)
#
# scoring.max_calls_per_run caps one fetch run; scoring.max_calls_per_day caps
# the day. Fetch runs three times a day, and Bespoke extraction makes model
# calls too, so a flooded queue (a big new company list, a lost seen file)
# could otherwise cost many times the usual figure in a day.
#
# The tally lives in fetch_budget.json, written by fetch runs only (AGENTS.md
# rule 9; daily.yml reads it for the banner and never writes it). One entry
# per local day in delivery.timezone, so the count resets at local midnight:
#
#   {"days": {"2026-11-09": {"scoring_calls": 412, "extractions": 37,
#                            "ceiling": 600, "held": 25, "reached": true}}}
#
# REV-07: "linkedin_runs" keeps the LinkedIn page counts of the last
# LINKEDIN_RUNS_KEPT fetch runs ({"day", "fetched", "blocked", "login_wall", "empty",
# "error", "kept", "unavailable"}), for doctor's blocked check and the
# digest's weekly "could not be read" line.
#
# scoring_calls counts API calls (a malformed reply's retry included);
# extractions counts Bespoke extraction calls. Both count toward the ceiling.
# When the day's total reaches it, the rest of the gate-passing postings are
# held unseen for a later run (dream companies scored first, as with the
# per-run cap) and Bespoke leaves its remaining pages for the next pass.

import json
import os

import profile_loader
from state import write_json_atomic

BUDGET_FILE = "fetch_budget.json"
KEEP_DAYS = 14
LINKEDIN_RUNS_KEPT = 30

# This run's Bespoke extraction calls, added by sources/bespoke.py before
# scoring starts; main.fetch_run records them with the scoring calls.
RUN = {"extractions": 0}


def reset_run():
    RUN["extractions"] = 0


def today():
    return profile_loader.local_today().isoformat()


def ceiling():
    return profile_loader.active()["scoring"]["max_calls_per_day"]


def load(path=None):
    path = path or BUDGET_FILE
    if not os.path.exists(path):
        return {"days": {}, "linkedin_runs": []}
    with open(path) as fh:
        data = json.load(fh)
    return {"days": dict(data.get("days") or {}), "linkedin_runs": list(data.get("linkedin_runs") or [])}


def _day(data, day):
    entry = data["days"].get(day) or {}
    return {"scoring_calls": int(entry.get("scoring_calls", 0)), "extractions": int(entry.get("extractions", 0)),
            "ceiling": entry.get("ceiling"), "held": int(entry.get("held", 0)),
            "reached": bool(entry.get("reached", False))}


def used_today(path=None):
    """Model calls already recorded today, plus this run's extractions so far."""
    entry = _day(load(path), today())
    return entry["scoring_calls"] + entry["extractions"] + RUN["extractions"]


def remaining_today(path=None):
    """Calls left under scoring.max_calls_per_day today (never below 0)."""
    return max(0, ceiling() - used_today(path))


def record(scoring_calls, extractions, held_by_ceiling, path=None):
    """Add this run's calls to today's entry and keep the last KEEP_DAYS days.
    held_by_ceiling > 0 marks the day as having reached the ceiling."""
    path = path or BUDGET_FILE
    data = load(path)
    day = today()
    entry = _day(data, day)
    entry["scoring_calls"] += scoring_calls
    entry["extractions"] += extractions
    entry["ceiling"] = ceiling()
    if held_by_ceiling:
        entry.update(held=held_by_ceiling, reached=True)
    data["days"][day] = entry
    for old in sorted(data["days"])[:-KEEP_DAYS]:
        del data["days"][old]
    write_json_atomic(path, data, indent=2)
    return entry


def ceiling_events(path=None):
    """[(day, held, ceiling)] for every recorded day that reached the ceiling,
    oldest first. Read only: the digest's banner uses it."""
    data = load(path)
    out = []
    for day in sorted(data["days"]):
        entry = _day(data, day)
        if entry["reached"] and entry["held"]:
            out.append((day, entry["held"], entry["ceiling"]))
    return out


def banner_line(day, held, limit):
    return (f"{held} posting{'s' if held != 1 else ''} {'are' if held != 1 else 'is'} waiting; "
            f"the daily scoring ceiling ({limit}) was reached on {day}")


# --- LinkedIn page counts (REV-07) ---------------------------------------------

def record_linkedin(counts, path=None):
    """Append one fetch run's LinkedIn page counts (sources/linkedin_email.py's
    commit, after the queue is written)."""
    path = path or BUDGET_FILE
    data = load(path)
    data["linkedin_runs"].append({"day": today(), **{k: int(v) for k, v in counts.items()}})
    data["linkedin_runs"] = data["linkedin_runs"][-LINKEDIN_RUNS_KEPT:]
    write_json_atomic(path, data, indent=2)


def linkedin_blocked_share(runs=3, path=None):
    """(failed pages, pages tried, runs counted) over the last `runs` LinkedIn
    runs that tried any page. Failed = blocked + login wall + empty + error."""
    recent = [r for r in load(path)["linkedin_runs"] if _tried(r)][-runs:]
    failed = sum(r.get(k, 0) for r in recent for k in ("blocked", "login_wall", "empty", "error"))
    return failed, sum(_tried(r) for r in recent), len(recent)


def _tried(run):
    return sum(run.get(k, 0) for k in ("fetched", "blocked", "login_wall", "empty", "error"))


def linkedin_unavailable_since(day, path=None):
    """LinkedIn jobs recorded as unavailable on or after `day` (ISO date)."""
    return sum(r.get("unavailable", 0) for r in load(path)["linkedin_runs"] if r.get("day", "") >= day)
