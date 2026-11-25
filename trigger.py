# trigger.py — REV-11: on-time runs through an external scheduler
#
# GitHub's scheduler starts this repository's cron lines hours late (2 to 11 h
# observed in November 2026) and sometimes skips one. An external scheduler
# (cron-job.org, or a Google Apps Script time-driven trigger) can instead call
# the workflow_dispatch API at the right minute with inputs.scheduled=true.
# The cron lines stay as the fallback: when both fire for one slot, the second
# finds nothing new.
#
#   python main.py trigger-setup     # the HTTP calls and the two setup recipes
#
# Three kinds of run (RUN_KIND, set by the workflows):
#   schedule  a cron line started it
#   trigger   workflow_dispatch with scheduled=true: counted as the scheduled run
#   manual    any other workflow_dispatch (a test or a catch-up run)
# Unset (a local run) reads as schedule, the behaviour before REV-11.

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"

RUN_KINDS = ("schedule", "trigger", "manual")
TOKEN_PLACEHOLDER = "<YOUR_GITHUB_TOKEN>"
API_VERSION = "2022-11-28"
LAG_WARN_MINUTES = 60
LAG_WARNING = "GitHub's scheduler is late; see python main.py trigger-setup"


def run_kind(env=None):
    """schedule | trigger | manual, from RUN_KIND (default schedule)."""
    kind = ((env if env is not None else os.environ).get("RUN_KIND") or "schedule").strip().lower()
    if kind not in RUN_KINDS:
        raise ValueError(f"RUN_KIND must be one of {', '.join(RUN_KINDS)}, not {kind!r}")
    return kind


def counts_as_scheduled(env=None):
    """A cron run or an external trigger: the run the skip counter and the
    alert-only email are about. A manual dispatch is not."""
    return run_kind(env) != "manual"


# --- Fetch: the cycle slot by the clock -------------------------------------------

def _circular_gap(a, b, period=24 * 60):
    d = abs(a - b) % period
    return min(d, period - d)


def clock_run_of_day(now, slots_utc):
    """Index of the fetch slot (config.FETCH_SLOTS_UTC, (hour, minute) UTC)
    nearest the time of day of UTC datetime `now`. A trigger fires on time, so
    the nearest slot is the one it was set up for, even an hour off in
    daylight time or a few minutes early."""
    minutes = now.hour * 60 + now.minute
    gaps = [_circular_gap(minutes, h * 60 + m) for h, m in slots_utc]
    return gaps.index(min(gaps))


def clock_shard(shard_state, now, slots_utc):
    """The startup cycle slot an on-time scheduled run takes at `now`: of the
    cycle slots that share this time of day's position (r, r + runs a day,
    ...), the one with the oldest last success. Daily vendors' boards are
    split by slot % runs-a-day (sources/startups.in_slot), so each time of day
    always fetches its own third of them."""
    runs_per_day = len(slots_utc)
    r = clock_run_of_day(now, slots_utc)
    last = shard_state["last_success"]
    candidates = range(r, shard_state["shards"], runs_per_day)
    return min(candidates, key=lambda i: (last.get(str(i), ""), i))


# --- trigger-setup ---------------------------------------------------------------

def repo_slug(run=subprocess.run):
    """owner/repo from GITHUB_REPOSITORY or the git remote origin, else a placeholder."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        out = run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$", out.stdout.strip())
        if m:
            return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "OWNER/REPO"


def _fmt(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _local_of_utc(hour, minute, tz, on):
    """HH:MM local for HH:MM UTC on date `on`."""
    t = datetime(on.year, on.month, on.day, hour, minute, tzinfo=timezone.utc).astimezone(tz)
    return f"{t:%H:%M}"


def jobs(delivery, lead_minutes, slots_utc):
    """The calls an external scheduler makes: [{name, workflow, when, days, tz}].
    The digest and weekly summary run in the profile's zone; the fetch slots in
    UTC, the times fetch.yml and the daily-source rule use."""
    import schedule
    s = schedule.slots(delivery, lead_minutes)
    tz = delivery["timezone"]
    out = [{"name": "digest", "workflow": "daily.yml", "when": _fmt(s["digest"].minutes),
            "days": list(s["digest"].days), "tz": tz}]
    for h, m in slots_utc:
        out.append({"name": f"fetch {h:02d}:{m:02d} UTC", "workflow": "fetch.yml", "when": f"{h:02d}:{m:02d}",
                    "days": list(schedule.CRON_DAYS), "tz": "UTC"})
    out.append({"name": "weekly summary", "workflow": "weekly_summary.yml", "when": _fmt(s["weekly"].minutes),
                "days": list(s["weekly"].days), "tz": tz})
    return out


def dispatch_url(repo, workflow):
    return f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"


BODY = '{"ref": "main", "inputs": {"scheduled": "true"}}'


def _days_text(days):
    import schedule
    if len(days) == 7:
        return "every day"
    return ", ".join(d for d in schedule.CRON_DAYS if d in days)


def apps_script(repo, delivery, lead_minutes, slots_utc):
    import schedule
    s = schedule.slots(delivery, lead_minutes)
    js_days = lambda days: "[" + ", ".join(str(schedule.CRON_DAYS.index(d)) for d in days) + "]"
    return f"""// Job digest on-time trigger (REV-11). Runs once an hour; dispatches what is due.
// Script Properties: GITHUB_TOKEN = the fine-grained token. Days: 0 = Sunday.
const REPO = '{repo}', TZ = '{delivery["timezone"]}';
const DIGEST = {{hour: {s["digest"].minutes // 60}, days: {js_days(s["digest"].days)}}};   // local
const WEEKLY = {{hour: {s["weekly"].minutes // 60}, days: {js_days(s["weekly"].days)}}};   // local
const FETCH_UTC_HOURS = [{", ".join(str(h) for h, _ in slots_utc)}];

function tick() {{
  const now = new Date();
  const day = Number(Utilities.formatDate(now, TZ, 'u')) % 7, hour = Number(Utilities.formatDate(now, TZ, 'H'));
  if (FETCH_UTC_HOURS.includes(now.getUTCHours())) dispatch('fetch.yml');
  if (hour === DIGEST.hour && DIGEST.days.includes(day)) dispatch('daily.yml');
  if (hour === WEEKLY.hour && WEEKLY.days.includes(day)) dispatch('weekly_summary.yml');
}}

function dispatch(workflow) {{
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  const res = UrlFetchApp.fetch('https://api.github.com/repos/' + REPO + '/actions/workflows/' + workflow + '/dispatches', {{
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    headers: {{Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json'}},
    payload: JSON.stringify({{ref: 'main', inputs: {{scheduled: 'true'}}}})}});
  if (res.getResponseCode() !== 204) throw new Error(workflow + ': HTTP ' + res.getResponseCode() + ' ' + res.getContentText());
}}"""


def setup_text(delivery, lead_minutes, slots_utc, repo, today=None):
    tz = ZoneInfo(delivery["timezone"])
    today = today or datetime.now(timezone.utc).date()
    calls = jobs(delivery, lead_minutes, slots_utc)
    lines = [
        "On-time delivery: an external trigger for the digest and fetch (REV-11)",
        "",
        "GitHub starts this repository's cron lines hours late and sometimes skips one. An external",
        "scheduler can start each run on time instead, through GitHub's workflow_dispatch API. The",
        "cron lines stay as the fallback; if both start the same slot, the second finds nothing new.",
        "",
        "1. The token (you create it; it lives only in the scheduler)",
        "   GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens >",
        "   Generate new token:",
        f"     Repository access: Only select repositories > {repo}",
        "     Permissions: Repository permissions > Actions: Read and write. Nothing else.",
        "     Expiration: one year (set a reminder to replace it).",
        "   Paste it only into the scheduler below. Never commit it, never add it to GitHub Secrets",
        "   or .env: the workflows do not need it.",
        "",
        "2. The calls",
        "   Every call is the same request to a different workflow:",
        f"     Method:  POST",
        f"     URL:     https://api.github.com/repos/{repo}/actions/workflows/<workflow>/dispatches",
        f"     Headers: Authorization: Bearer {TOKEN_PLACEHOLDER}",
        f"              Accept: application/vnd.github+json",
        f"     Body:    {BODY}",
        "   GitHub answers 204 No Content when the run is queued.",
        "",
    ]
    width = max(len(c["name"]) for c in calls)
    for c in calls:
        local = ""
        if c["tz"] == "UTC":
            h, m = (int(x) for x in c["when"].split(":"))
            winter = _local_of_utc(h, m, tz, today.replace(month=1, day=15))
            summer = _local_of_utc(h, m, tz, today.replace(month=7, day=15))
            local = f" ({winter}{'' if winter == summer else ' or ' + summer} {delivery['timezone']})"
        lines.append(f"   {c['name']:<{width}}  {c['when']} {c['tz']}{local}, {_days_text(c['days'])}")
        lines.append(f"   {'':<{width}}  POST {dispatch_url(repo, c['workflow'])}")
    lines += [
        "",
        f"   The digest call is delivery.digest_time_local ({delivery['digest_time_local']}) minus "
        f"config.CRON_LEAD_MINUTES ({lead_minutes} min).",
        "   The fetch calls are in UTC so they match fetch.yml and the once-a-day sources rule; an",
        "   on-time fetch takes the startup slot for its time of day.",
        "",
        "3. Option A (default): cron-job.org, free",
        "   Sign up at https://cron-job.org, then for each line above: Create cronjob.",
        "     Title:     job digest: <name>",
        "     URL:       the POST URL for that line",
        "     Schedule:  Custom; the time and days for that line; Time zone: the zone shown",
        "                (UTC for the three fetch jobs)",
        "     Advanced > Request method: POST",
        f"     Advanced > Headers: Authorization = Bearer <the token>; Accept = application/vnd.github+json",
        f"     Advanced > Request body: {BODY}",
        "     Save, then Test run on the fetch job once: GitHub > Actions should show a",
        "     Job Fetch run named \"(external trigger)\".",
        f"   That is {len(calls)} jobs. cron-job.org runs them within a minute and keeps a history of each answer.",
        "",
        "4. Option B: Google Apps Script, free",
        "   https://script.google.com > New project, paste the script below.",
        "   Project Settings > Script Properties > Add: GITHUB_TOKEN = the token.",
        "   Triggers > Add Trigger: function tick, time-driven, hour timer, every hour.",
        "   Apps Script picks the minute within each hour, so runs land up to an hour after the",
        f"   times above; the digest's {lead_minutes}-minute lead absorbs that.",
        "",
    ]
    lines += ["   " + l if l else "" for l in apps_script(repo, delivery, lead_minutes, slots_utc).splitlines()]
    lines += [
        "",
        "5. Check it",
        "   The next digest run shows in GitHub > Actions as event workflow_dispatch, named",
        "   \"Job Digest Agent (external trigger)\", and its log prints",
        "   \"[Run] scheduled run (external trigger)\". python main.py doctor reports GitHub's lag.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None):
    import config
    import profile_loader
    if argv:
        raise SystemExit("usage: python main.py trigger-setup")
    delivery = profile_loader.active()["delivery"]
    print(setup_text(delivery, config.CRON_LEAD_MINUTES, config.FETCH_SLOTS_UTC, repo_slug()), end="")
    return 0


# --- doctor: GitHub's scheduler lag ----------------------------------------------

def workflow_crons(path):
    """The cron lines of a workflow file."""
    return re.findall(r"^\s*-\s*cron:\s*['\"]([^'\"]+)['\"]", Path(path).read_text(), flags=re.M)


def _matches(cron, moment):
    """'M H * * DOW' with H and DOW as lists or ranges, M a number; the shapes
    the workflows carry ('17 4,12,20 * * *', '17 7 * * 0-5')."""
    minute, hour, _, _, dow = cron.split()

    def field(text, top):
        if text == "*":
            return set(range(top))
        vals = set()
        for part in text.split(","):
            lo, _, hi = part.partition("-")
            vals.update(range(int(lo), int(hi or lo) + 1))
        return vals
    return (moment.minute in field(minute, 60) and moment.hour in field(hour, 24)
            and (moment.weekday() + 1) % 7 in field(dow, 7))


def cron_instant(crons, started):
    """The latest minute at or before `started` that any of `crons` matches
    (within two days), or None."""
    t = started.astimezone(timezone.utc).replace(second=0, microsecond=0)
    for _ in range(2 * 24 * 60):
        if any(_matches(c, t) for c in crons):
            return t
        t -= timedelta(minutes=1)
    return None


def run_lags(runs, crons_by_path):
    """[(workflow name, started, lag minutes)] for scheduled runs (newest first).
    Lag = start time minus the latest cron minute at or before it. With more
    than one line a day (fetch's three, a DST twin) a run later than the gap
    to the next line reads as the nearer line: a floor, never an overstatement."""
    out = []
    for run in runs:
        crons = crons_by_path.get(run.get("path", ""))
        started = run.get("run_started_at") or run.get("created_at")
        if not crons or not started:
            continue
        started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        fired = cron_instant(crons, started)
        if fired is None:
            continue
        out.append((run.get("name", run.get("path")), started, round((started - fired).total_seconds() / 60)))
    return out


def fetch_schedule_runs(run=subprocess.run, per_page=5):
    """The repository's last scheduled runs, through gh (list of run dicts)."""
    import json
    out = run(["gh", "api", f"repos/{repo_slug()}/actions/runs?event=schedule&per_page={per_page}"],
              cwd=ROOT, capture_output=True, text=True, timeout=20, env=dict(os.environ, GH_PROMPT_DISABLED="1"))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:160] or "no output")
    return json.loads(out.stdout).get("workflow_runs", [])


def crons_by_path():
    return {f".github/workflows/{p.name}": workflow_crons(p) for p in WORKFLOW_DIR.glob("*.yml")
            if workflow_crons(p)}
