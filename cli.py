# cli.py — the commands main.py runs before its pipeline imports (PUB-06)
#
# scorer.py and several sources read their API keys at import time, so any
# command that must work on a fresh clone with no secrets set -- help,
# profile-check, cron, slot, doctor, setup, sheet-init -- is dispatched from here, before
# main.py imports the pipeline. calibrate and digest --dry-run also start here
# and import only the pieces they need.
#
#   python main.py --help

import os
import sys

HELP = """usage: python main.py <command> [options]

Pipeline (GitHub Actions runs these):
  fetch          fetch, score and queue new postings (one cycle slot; --daily-sources adds the daily ones)
  digest         email and log every queued posting, then dequeue it (--scheduled: slot guard first)
  all            fetch then digest in one run (local use)

Setup and checks (no secrets needed unless noted):
  profile-check  validate profile.yaml and print what it configures
  cron           print the workflow schedule blocks for this profile and a diff (--write rewrites them)
  doctor         check Python, files, credentials and config (--phase 1|2|3, --json); read-only
  setup          interview in the terminal and write profile.yaml (--force to replace one;
                   --from-document PATH proposes defaults from a resume or job description, one model call)
  resolve        find boards for the companies in the profile's company lists: a preview first, then Continue? (--yes, --list, --max, --report-only, --guess-websites, --workdir)
  sheet-init     create the Sheet's two tabs and header rows (GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON)
  calibrate      rank postings under this profile (--json):
                   --titles [--sources a,b] [--max N]  live postings through the title gate and country filter; no model calls
                   [--sources a,b] [--max N]           live postings, scored (default 15); needs ANTHROPIC_API_KEY
                   --dir examples/postings                  the sample postings (or any folder); needs ANTHROPIC_API_KEY
  digest --dry-run  render the digest to out/digest_dry_run.html; changes no state (--from-calibrate, --send, --with-failed-runs FILE)

Workflow internals:
  slot digest|weekly  is this scheduled run the intended slot? (the workflows' first job)
"""


def _profile_check():
    import config
    import profile_loader
    import schedule
    status = profile_loader.check()
    if status == 0:
        delivery = profile_loader.active()["delivery"]
        found = schedule.lines(delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES)
        stale = schedule.mismatches(delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES)
        for name, file in (("digest", "daily.yml"), ("weekly", "weekly_summary.yml")):
            crons = ", ".join(f"'{l.cron}'" for l in found[name])
            state_text = "out of date: run python main.py cron --write" if name in stale else "matches"
            print(f"Cron:        {file} {crons} ({state_text})")
    return status


def _cron(write):
    """PUB-05/PUB-08: print the schedule blocks daily.yml and weekly_summary.yml
    should carry for this profile and a diff against what they carry; with
    write, rewrite them."""
    import difflib

    import config
    import profile_loader
    import schedule
    delivery = profile_loader.active()["delivery"]
    args = (delivery, config.CRON_LEAD_MINUTES, config.CRON_OFF_THE_HOUR_MINUTES)
    if write:
        changed = schedule.write(*args)
        for name, path in schedule.WORKFLOWS.items():
            print(f"{path.relative_to(schedule.ROOT)}: {'rewritten' if name in changed else 'already up to date'}")
        return 0
    stale = schedule.mismatches(*args)
    for name, path in schedule.WORKFLOWS.items():
        rel = str(path.relative_to(schedule.ROOT))
        print(f"{rel}:\n{schedule.block(name, *args)}", end="")
        if name in stale:
            have, want = stale[name]
            print("".join(difflib.unified_diff(have.splitlines(True), want.splitlines(True),
                                               f"{rel} (now)", f"{rel} (generated)")), end="")
            print("  -> differs: run python main.py cron --write\n")
        else:
            print("  -> the file matches\n")
    return 0


def _guard(name):
    import config
    import profile_loader
    import schedule
    return schedule.slot_guard(name, profile_loader.active()["delivery"], config.CRON_LEAD_MINUTES,
                               config.SLOT_GUARD_MINUTES, cron=os.environ.get("SCHEDULED_CRON") or None,
                               off_the_hour=config.CRON_OFF_THE_HOUR_MINUTES)


def _slot(name):
    """PUB-06: the workflows' first job. Writes run=true|false to
    $GITHUB_OUTPUT (or prints it) and exits 0 either way."""
    line = f"run={'true' if _guard(name) else 'false'}"
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as fh:
            fh.write(line + "\n")
    print(line)
    return 0


def early(argv):
    """Run `argv` if it is one of the commands handled here and return its
    exit code; None means main.py goes on to its pipeline (with argv possibly
    narrowed: --scheduled is consumed by the slot guard). A billing/auth error
    from a model call (calibrate, setup --from-document, resolve --guess-websites)
    ends the command with the OPS-12 line and exit 1 (PUB-14)."""
    try:
        return _early(argv)
    except Exception as e:
        # llm is imported only by a command that makes model calls (the slot
        # guard runs from a sparse checkout without it).
        llm = sys.modules.get("llm")
        if llm is None or not isinstance(e, llm.ApiAccessError):
            raise
        print(f"::error title=Anthropic API access failed::{e.cause}")
        return 1


def _early(argv):
    if not argv or argv[0] in ("fetch", "all") or argv == ["digest"] or argv[:1] == ["--daily-sources"]:
        return None
    cmd, rest = argv[0], argv[1:]
    if cmd in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    if argv == ["profile-check"]:
        return _profile_check()
    if cmd == "cron" and rest in ([], ["--write"]):
        return _cron(write=bool(rest))
    if cmd == "slot" and rest in (["digest"], ["weekly"]):
        return _slot(rest[0])
    if cmd == "doctor":
        import doctor
        return doctor.main(rest)
    if cmd == "resolve":
        import resolve
        return resolve.main(rest)
    if cmd == "sheet-init":
        import sheet_init
        return sheet_init.main(rest)
    if cmd == "setup":
        import setup_interview
        return setup_interview.main(rest)
    if cmd == "calibrate":
        import calibrate
        return calibrate.main(rest)
    if cmd == "digest" and "--dry-run" in rest:
        import dry_run
        return dry_run.main([a for a in rest if a != "--dry-run"])
    # PUB-08: a run a schedule: trigger started may pass --scheduled. Every
    # cron line of a DST zone fires, so the guard lets through only the one
    # meant for today and the other exits 0 here, before any pipeline import
    # reads a key, a file or the network. Since PUB-06 the workflows run the
    # guard in their own first job (`slot`); the flag stays for local use.
    if argv == ["digest", "--scheduled"]:
        if not _guard("digest"):
            return 0
        sys.argv.remove("--scheduled")
        return None
    if "--scheduled" in argv:
        sys.exit("usage: python main.py digest --scheduled  (--scheduled is for the digest only)")
    return None
