# dry_run.py — `python main.py digest --dry-run`: see the digest without running it (PUB-06)
#
#   python main.py digest --dry-run                    # from the queue in state/pending/
#   python main.py digest --dry-run --from-calibrate   # from a calibrate run (a fresh install has no queue)
#   python main.py digest --dry-run --send             # also email it, subject prefixed "[DRY RUN]"
#
# Builds the digest HTML the real digest would send and writes it to
# out/digest_dry_run.html. It deletes nothing from the queue, writes nothing
# to the Sheet, skip_state.json or any cache, and runs no Applied-seed or
# status pass. Differences from the real digest: no Sheet read, so the FIL-13
# collapse sees only this batch; no company-profile blurb rewrite (DEL-14,
# which calls Claude and writes company_profiles.json); no fetch-failure banner.

import argparse
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out", "digest_dry_run.html")
SUBJECT_PREFIX = "[DRY RUN] "


def _allow_import_without(*names):
    """scorer.py and emailer.py read these at import. A dry run calls neither
    Claude nor (without --send) Gmail, so an unset one is set empty for this
    process only; --send checks for the real values first."""
    for name in names:
        os.environ.setdefault(name, "")


def sections_from_queue():
    """(sections, note) from state/pending/, read only."""
    import pending
    import state
    from scorer import filter_jobs
    paths, queued, first_run = pending.load_pending()
    for job in queued:
        state.name_employer(job)                 # in memory, as the digest does on load
    scored = pending.dedupe(queued)
    pending.retier_legacy(scored)                # in memory; batch files are never rewritten
    cards = pending.collapse_hiring(scored, recent_rows=[])
    note = (f"{len(paths)} queued batches, {len(queued)} postings, {len(scored)} after dedup, "
            f"{len(cards)} cards (no Sheet read: the collapse saw this batch only)")
    if first_run:
        cards = sorted(cards, key=lambda j: j.get("score", 0), reverse=True)[:10]
    return filter_jobs(cards), first_run, note


def sections_from_calibrate(folder=None):
    import calibrate
    from scorer import filter_jobs
    ranked, calls = calibrate.run(folder or calibrate.DEFAULT_DIR)
    jobs = [j for j in ranked if not j.get("international")]
    for job in jobs:
        job.setdefault("date_posted", "")
    api = calibrate.LAST_USAGE.get("calls", calls)
    return filter_jobs(jobs), False, f"{len(jobs)} calibrate postings ({calls} scored, {api} Claude calls)"


def build(sections, first_run=False):
    """(subject, html) exactly as emailer.send_digest would build them."""
    import emailer
    main, worth = sections.get("main", []), sections.get("worth_a_look", [])
    total = len(main) + len(worth)
    subject = f"{SUBJECT_PREFIX}Job digest — {total} match{'es' if total != 1 else ''} · {emailer._today()}"
    html = emailer._build_html(main, worth, sections.get("cool_wrong_role", []), [], first_run, False)
    return subject, html


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python main.py digest --dry-run",
                                     description="Render the digest to out/digest_dry_run.html; change nothing (PUB-06)")
    parser.add_argument("--from-calibrate", action="store_true",
                        help="render the calibrate results instead of the queue (makes Claude calls)")
    parser.add_argument("--send", action="store_true",
                        help="also email the file to delivery.email_to, subject prefixed [DRY RUN]")
    args = parser.parse_args(argv)

    if args.send:
        missing = [n for n in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD") if not os.environ.get(n, "").strip()]
        if missing:
            print(f"{', '.join(missing)} missing: --send needs the Gmail account and its app password.")
            return 1
    if args.from_calibrate and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY missing: --from-calibrate scores the sample postings with Claude.")
        return 1
    _allow_import_without("ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")

    sections, first_run, note = sections_from_calibrate() if args.from_calibrate else sections_from_queue()
    subject, html = build(sections, first_run)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write(html)
    counts = (f"top matches {len(sections['main'])}, worth a look {len(sections['worth_a_look'])}, "
              f"cool company/wrong role {len(sections['cool_wrong_role'])}")
    print(f"[DryRun] {note}")
    print(f"[DryRun] {counts}")
    print(f"[DryRun] wrote {os.path.relpath(OUT, ROOT)}; queue, Sheet and state files untouched")
    if args.send:
        import emailer
        emailer._send(subject, html)
        print(f"[DryRun] emailed to {emailer.RECIPIENT_EMAIL}: {subject}")
    return 0
