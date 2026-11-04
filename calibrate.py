# calibrate.py — `python main.py calibrate`: how does this profile rank postings? (PUB-06)
#
#   python main.py calibrate --dir examples/postings   # the twelve sample postings
#   python main.py calibrate --dir my_postings    # any folder of .md / .txt postings
#   python main.py calibrate --titles             # live postings: title gate + country filter, no model calls
#   python main.py calibrate                      # live postings: score up to --max (default 15)
#   python main.py calibrate --sources ats,adzuna # live, from these sources (slow ones only when named)
#   python main.py calibrate --json               # the same results as JSON
#
# Scores every posting in the folder under the loaded profile with the
# pipeline's own steps: the outside-the-country location filter, the hard-block
# name pre-filter, one scoring call per posting (scorer.score_jobs), then the
# code modifiers (work model, location tier, home bonus, comp, function cap,
# floor and cap; any of them may be a dealbreaker).
# Prints a ranked table and, below it, which profile fields moved each
# posting. Writes nothing anywhere -- no Sheet, no seen_jobs.json, no queue --
# and prints how many Claude calls it made (one per posting, plus any retry
# after a malformed reply) and how many postings they scored.
#
# A posting file is a "# Title" line, then "Key: value" lines (Company,
# Location, Work model, Pay), a blank line, and the description.

import argparse
import builtins
import contextlib
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(ROOT, "examples", "postings")
FIELDS = {"company": "company", "location": "location", "work model": "work_model", "pay": "comp",
          "source": "display_source"}
# The industries tiers the "what drove the ranking" block looks for, in the
# order the prompt weighs them.
TIERS = ("strong_yes", "lean_in", "neutral", "lowest_interest", "deprioritize")


def parse_posting(path):
    """A sample file as the job dict a source would hand the pipeline."""
    with open(path) as fh:
        lines = fh.read().splitlines()
    job = {"title": "", "company": "", "location": "", "work_model": "", "comp": "",
           "source": "calibrate", "apply_link": f"calibrate://{os.path.basename(path)}",
           "file": os.path.basename(path)}
    body_at = len(lines)
    for i, line in enumerate(lines):
        if line.startswith("# ") and not job["title"]:
            job["title"] = line[2:].strip()
            continue
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() in FIELDS and not line.startswith(" "):
            job[FIELDS[key.strip().lower()]] = value.strip()
            continue
        if not line.strip() and job["title"]:
            body_at = i + 1
            break
    job["description"] = "\n".join(lines[body_at:]).strip()
    if job.get("display_source"):
        # PUB-18: a sample may name the source it stands in for ("Source: ats"),
        # so the digest badge and the Sheet's Source column read like a real
        # run; its apply link is then a plausible one on example.com.
        from config import source_key
        job["source"] = source_key(job.pop("display_source"))
        job["apply_link"] = f"https://example.com/jobs/{os.path.splitext(job['file'])[0]}"
    return job


def load_postings(folder):
    names = sorted(f for f in os.listdir(folder) if f.endswith((".md", ".txt")))
    return [parse_posting(os.path.join(folder, f)) for f in names]


def run(folder=DEFAULT_DIR):
    """(scored jobs in rank order, postings scored) for the postings in `folder`.
    LAST_USAGE["calls"] has the API calls, retries included."""
    return score(load_postings(folder))


def score(postings):
    """(scored jobs in rank order, postings scored): the pipeline's own
    steps on these job dicts. Each needs a unique "file" (a sample's file
    name, a live posting's link) to break ties."""
    import location
    import profile_loader
    import scorer

    dreams = {n.casefold() for n in profile_loader.dream_companies()}
    jobs, filtered = [], []
    for job in postings:
        job["is_dream_company"] = job["company"].casefold() in dreams
        if not location.passes_location_filter(job):
            # The pipeline drops these before scoring (location.outside_country).
            job.update(score=0, base_score=0, rationale=f"Outside {location.CONFIG.country}: dropped by the "
                                                        f"location filter before scoring.", international=True)
            filtered.append(job)
        else:
            jobs.append(job)

    calls = {"n": 0}
    usage_before = dict(scorer.USAGE)
    real = scorer._score_single

    def counted(job, feedback_block=""):
        calls["n"] += 1
        return real(job, feedback_block)

    scorer._score_single = counted
    try:
        scored = scorer.score_jobs(jobs)
    finally:
        scorer._score_single = real
        location.reset_location_resolutions()
    ranked = sorted(scored + filtered, key=lambda j: (-j.get("score", 0), -j.get("base_score", j.get("score", 0)),
                                                      j["file"]))
    LAST_USAGE.clear()
    LAST_USAGE.update({k: scorer.USAGE[k] - usage_before[k] for k in scorer.USAGE}, model=scorer.MODEL)
    return ranked, calls["n"]


# PUB-12: the scoring calls of the last score() call that the API answered,
# with their tokens and the model: {"calls", "input", "output", "model"}.
LAST_USAGE = {}


def cost_summary(usage, per_day=None):
    """{calls, avg_input, avg_output, run, per_call, per_100, per_month} from
    the API's usage fields; costs None when config.MODEL_PRICES lacks the model."""
    import config
    calls = usage.get("calls", 0)
    out = {"model": usage.get("model", ""), "calls": calls, "avg_input": 0, "avg_output": 0,
           "run": None, "per_call": None, "per_100": None, "per_day": per_day, "per_month": None}
    if not calls:
        return out
    out["avg_input"] = round(usage["input"] / calls)
    out["avg_output"] = round(usage["output"] / calls)
    run_cost = config.model_cost(out["model"], usage["input"], usage["output"])
    if run_cost is not None:
        out.update(run=run_cost, per_call=run_cost / calls, per_100=run_cost / calls * 100)
        if per_day:
            out["per_month"] = run_cost / calls * per_day * 30
    return out


def cost_lines(cost):
    """The lines calibrate prints after the table."""
    from config import format_cost
    if not cost["calls"]:
        return ["Cost: no scoring call was answered, so there is nothing to measure."]
    lines = [f"Cost: {cost['calls']} scoring calls at {cost['model']}, on average {cost['avg_input']:,} input "
             f"and {cost['avg_output']:,} output tokens a call; this run {format_cost(cost['run'])} "
             f"({format_cost(cost['per_call'])} a call)."]
    if cost["per_call"] is None:
        lines.append(f"  {cost['model']} is not in config.MODEL_PRICES, so its cost is unknown.")
        return lines
    rate = f"At this rate, 100 scored postings ≈ {format_cost(cost['per_100'])}"
    if cost["per_day"]:
        rate += (f"; a typical month at {cost['per_day']} a day ≈ {format_cost(cost['per_month'])} "
                 f"({cost['per_day']} = the postings this live fetch passed)")
    lines.append(rate + ". Prices change; config.MODEL_PRICES is dated.")
    return lines


# --- Live postings (PUB-10) ------------------------------------------------------
#
# `calibrate --titles` and `calibrate` without --dir fetch recent postings
# from the sources with the profile's own queries. The fast API sources run by
# default when sources.enabled turns them on; the slow crawlers only when named
# in --sources. Nothing fetched is recorded: every state file the pipeline
# keeps is guarded against writes for the whole run (no_state_writes), and
# Bespoke's state saves are switched off.

FAST_SOURCES = ("adzuna", "jsearch", "builtin", "usajobs")
SLOW_SOURCES = ("ats", "startups", "bespoke", "watchlist")
# Beyond the source's own secrets in envfile.SECRETS.
EXTRA_KEYS = {"usajobs": ("GMAIL_ADDRESS",), "bespoke": ("ANTHROPIC_API_KEY",)}
DEFAULT_MAX = 15
# The state files and the queue: a live calibrate never opens any for writing.
STATE_NAMES = ("seen_jobs.json", "skip_state.json", "bespoke_state.json", "bespoke_cursor.json",
               "shard_state.json", "status_email_cache.json", "company_profiles.json")


def _protected(path):
    full = os.path.abspath(os.fspath(path))
    name = os.path.basename(full)
    return (any(name == n or name.startswith(n + ".") for n in STATE_NAMES)
            or full.startswith(os.path.join(ROOT, "state", "pending") + os.sep))


@contextlib.contextmanager
def no_state_writes():
    """Any open-for-writing or os.replace onto a state file or state/pending/
    raises PermissionError while this is active."""
    real_open, real_replace = builtins.open, os.replace

    def guarded_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and set(mode) & set("wax+") and _protected(file):
            raise PermissionError(f"calibrate never writes {os.fspath(file)}")
        return real_open(file, mode, *args, **kwargs)

    def guarded_replace(src, dst, *args, **kwargs):
        if _protected(dst):
            raise PermissionError(f"calibrate never writes {os.fspath(dst)}")
        return real_replace(src, dst, *args, **kwargs)

    builtins.open = io.open = guarded_open
    os.replace = guarded_replace
    try:
        yield
    finally:
        builtins.open = io.open = real_open
        os.replace = real_replace


def required_keys(name):
    import envfile
    return tuple(s.name for s in envfile.SECRETS if s.needed == f"sources.enabled.{name}") + EXTRA_KEYS.get(name, ())


def pick_sources(named, enabled):
    """(sources to fetch, notes). Named: exactly those (fast or slow); none
    named: the fast sources the profile enables."""
    notes = []
    if named:
        unknown = [n for n in named if n not in FAST_SOURCES + SLOW_SOURCES]
        if unknown:
            raise SystemExit(f"--sources: unknown {', '.join(unknown)}; choose from "
                             f"{', '.join(FAST_SOURCES + SLOW_SOURCES)}")
        picked = list(dict.fromkeys(named))
        for n in picked:
            if not enabled.get(n):
                notes.append(f"[{n}] disabled in profile.yaml (sources.enabled) — fetched anyway because it was named")
            if n in SLOW_SOURCES:
                notes.append(f"[{n}] is a slow source: it crawls many boards and can take several minutes")
    else:
        picked = [n for n in FAST_SOURCES if enabled.get(n)]
        notes.append(f"slow sources ({', '.join(SLOW_SOURCES)}) run only when named in --sources")
    return picked, notes


def _fetcher(name):
    """The source's fetch_all, with Bespoke's state saves switched off."""
    import importlib
    module = importlib.import_module(f"sources.{name}")
    if name == "bespoke":
        module.save_state = lambda *a, **k: print("[Calibrate] bespoke state not saved (calibrate writes nothing)")
        module.save_cursor = lambda *a, **k: None
        return lambda: list(module.fetch_all())
    if name == "startups":
        return lambda: list(module.fetch_all())
    return module.fetch_all


def fetch_live(names, fetchers=None, log=print):
    """{source: [job]} and {source: why it did not run}."""
    got, skipped = {}, {}
    for name in names:
        missing = [k for k in required_keys(name) if not os.environ.get(k, "").strip()]
        if missing and not (fetchers and name in fetchers):
            import envfile
            skipped[name] = "; ".join(f"{k} missing ({envfile.secret(k).where})" for k in missing)
            continue
        try:
            fetch = (fetchers or {}).get(name) or _fetcher(name)
            got[name] = [dict(j, source=j.get("source") or name) for j in fetch()]
        except PermissionError:
            raise
        except Exception as e:                         # noqa: BLE001 -- one broken source never hides the rest
            skipped[name] = f"failed: {type(e).__name__}: {' '.join(str(e).split())[:160]}"
    return got, skipped


def gate(got):
    """(passed, failed): the title gate (sources in roles.title_gated_sources)
    and the country filter, as the fetch run applies them. failed carries
    job["calibrate_failed"]: "title gate" or "outside <country>"."""
    import config
    import location
    passed, failed, seen = [], [], set()
    for name, jobs in got.items():
        gated = name in config.TITLE_GATED_SOURCES
        for job in jobs:
            title = (job.get("title") or "").strip()
            if gated and not config.title_gate(title):
                failed.append(dict(job, calibrate_failed="title gate"))
                continue
            if not location.passes_location_filter(job):
                failed.append(dict(job, calibrate_failed=f"outside {location.CONFIG.country}"))
                continue
            key = (job.get("company", "").casefold().strip(), title.casefold())
            if key in seen:
                continue                               # the same posting from two sources: list it once
            seen.add(key)
            passed.append(job)
    return passed, failed


def round_robin(jobs, limit):
    """Up to `limit` jobs, taking one from each source in turn."""
    by_source = {}
    for job in jobs:
        by_source.setdefault(job["source"], []).append(job)
    out = []
    while len(out) < limit and any(by_source.values()):
        for queue in by_source.values():
            if queue and len(out) < limit:
                out.append(queue.pop(0))
    return out


def render_titles(got, skipped, passed, failed, names, limit=None):
    """The --titles report: passed and failed lists, then counts per source."""
    lines = [f"Live postings from {', '.join(names) or 'no source'}: title gate and country filter only "
             f"(no model calls; nothing written)."]
    for header, jobs, with_reason in (("PASSED", passed, False), ("FAILED", failed, True)):
        lines += ["", f"{header} ({len(jobs)})"]
        shown = jobs if limit is None else round_robin(jobs, limit)
        cols = [("source", 9)] + ([("reason", 14)] if with_reason else []) + [("title", 52), ("company", 30)]
        lines.append("  " + "  ".join(f"{h:<{w}}" for h, w in cols))
        for job in shown:
            values = [job.get("source", "")] + ([job["calibrate_failed"]] if with_reason else []) + \
                     [job.get("title", ""), job.get("company", "")]
            lines.append("  " + "  ".join(f"{_cut(v, w):<{w}}" for v, (_, w) in zip(values, cols)))
        if len(shown) < len(jobs):
            lines.append(f"  … {len(jobs) - len(shown)} more (raise --max to list them)")
    lines += ["", "Per source:", f"  {'source':<10}{'fetched':>8}{'passed':>8}{'title':>8}{'country':>9}"]
    for name in names:
        if name in skipped:
            lines.append(f"  {name:<10}  not run: {skipped[name]}")
            continue
        fetched = len(got.get(name, []))
        p = sum(1 for j in passed if j["source"] == name)
        t = sum(1 for j in failed if j["source"] == name and j["calibrate_failed"] == "title gate")
        c = sum(1 for j in failed if j["source"] == name and j["calibrate_failed"] != "title gate")
        lines.append(f"  {name:<10}{fetched:>8}{p:>8}{t:>8}{c:>9}")
    lines.append("  (passed counts each posting once; a repeat from a second source is not listed again)")
    return "\n".join(lines)


def adjustments(job):
    """The code adjustments as "+1 home, -2 comp" style text."""
    import location
    if job.get("dealbreaker"):
        return f"dealbreaker: {job['dealbreaker']}"
    parts = []
    if job.get("work_model_adjustment"):
        parts.append(f"{job['work_model_adjustment']:+d} work model")
    if job.get("location_penalty"):
        parts.append(f"{job['location_penalty']:+d} location")
    if job.get("home_bonus"):
        parts.append(f"+{job['home_bonus']} home")
    if job.get("comp_adjustment"):
        parts.append(f"{job['comp_adjustment']:+d} comp")
    base = job.get("base_score", 0)
    raw = base + sum(job.get(k) or 0 for k in ("work_model_adjustment", "location_penalty", "home_bonus",
                                               "comp_adjustment"))
    if job.get("function_capped"):
        cap = location.CONFIG.function_cap
        parts.append(f"cap {cap} (function)" if job.get("score", 0) < raw else f"function cap {cap} (not binding)")
    elif base > 0 and job.get("score", 0) < raw:
        parts.append(f"cap {location.CONFIG.max_score}")
    elif base > 0 and job.get("score", 0) > raw:
        parts.append(f"floor {location.CONFIG.score_floor}")
    return ", ".join(parts) or "-"


def _money(n):
    return f"${n:,.0f}"


def _tier_field(reason):
    """The location field behind a tier reason."""
    return ("location.relocate" if reason.startswith("would-relocate") else
            "location.outer_band_miles" if reason.startswith("near commute") else
            "location.state_tier_overrides" if reason.startswith("state override") else
            "location.outside_country" if reason.startswith("outside ") else
            "location.elsewhere_penalty")


def drivers(job, profile):
    """The profile fields that moved this posting, one phrase each."""
    import comp
    import location
    loc, pay = profile["location"], profile["compensation"]
    out = []
    if job.get("international"):
        return [f"location.outside_country: {loc['outside_country']} (outside {loc['country']})"]
    if job.get("hard_block_industry"):
        how = "name pre-filter" if str(job.get("rationale", "")).startswith("Auto-disqualified") else "scoring call"
        out.append(f"industries.hard_block: {job['hard_block_industry']} ({how})")
        return out
    # PUB-09: the scoring.role_criteria the scoring call found, by name.
    points = {c["id"]: c.get("points") for c in profile["scoring"]["role_criteria"]}
    hits = job.get("criteria_hits") or []
    if hits:
        out.append("scoring.role_criteria: " + ", ".join(
            f"{h} (dealbreaker)" if h in points and not points[h] else
            f"{h} ({points[h]:+d})" if h in points else h for h in hits))
    elif job.get("base_score"):
        out.append("scoring.role_criteria: none named by the scoring call")
    # PUB-19: a dealbreaker the call reported but the code could not verify.
    for miss in job.get("dealbreaker_unverified") or ():
        quote = f' (quoted: "{_cut(miss["quote"], 90)}")' if miss.get("quote") else ""
        out.append(f"scoring.role_criteria.{miss['id']}: dealbreaker reported without verifiable evidence; "
                   f"ignored: {miss['reason']}{quote}")
    reason = job.get("location_reason", "")
    deal = job.get("dealbreaker")
    if deal:
        key = location.work_model_key(job)
        field = (f"scoring.role_criteria.{job['dealbreaker_criterion']}" if job.get("dealbreaker_criterion") else
                 f"location.work_models.{key}" if reason == f"dealbreaker: {key}" else
                 "compensation.floor_adjustment" if deal == "below floor" else
                 "compensation.unstated" if deal == "no stated pay" else
                 "location.outside_country" if deal.startswith("outside ") else _tier_field(deal))
        out.append(f"{field}: disqualify (dealbreaker: {deal})")
        return out
    if job.get("work_model_adjustment"):
        out.append(f"location.work_models.{location.work_model_key(job)}: {job['work_model_adjustment']:+d}")
    if job.get("location_penalty"):
        out.append(f"{_tier_field(reason)}: {reason.split(', home bonus')[0]} ({job['location_penalty']:+d})")
    if job.get("home_bonus"):
        out.append(f"location.home: {loc['home']} (+{job['home_bonus']} within {loc['home_bonus_radius_miles']} mi)")
    if job.get("comp_adjustment") and comp.stated_comp_top(job) is None:
        out.append(f"compensation.unstated: no stated pay ({job['comp_adjustment']:+d})")
    elif job.get("comp_adjustment"):
        top = comp.stated_comp_top(job)
        field = "compensation.stretch" if job["comp_adjustment"] > 0 else "compensation.floor"
        threshold = pay["stretch"] if job["comp_adjustment"] > 0 else pay["floor"]
        out.append(f"{field}: {_money(threshold)} vs a stated top of {_money(top) if top else '?'} "
                   f"({job['comp_adjustment']:+d})")
    import config
    fit = config.function_fit_value(job.get("function_fit"))
    if job.get("function_capped"):
        out.append(f"scoring.function_cap: {profile['scoring']['function_cap']} (function_fit {fit})")
    elif fit and fit != config.FUNCTION_FIT_LEADERSHIP:
        out.append(f"function_fit {fit}: the scoring call scored it {job.get('base_score', 0)} itself, "
                   f"so scoring.function_cap ({profile['scoring']['function_cap']}) had nothing to cap")
    if job.get("is_dream_company"):
        out.append("dream_companies: listed (main digest from scoring.thresholds.dream_company_main)")
    # The blurb's first sentence describes the company; the rest describes the
    # role, where "data centers" or "hardware" are duties, not the sector.
    from location import split_rationale
    blurb = split_rationale(job.get("rationale"))[0]
    first_sentence = re.split(r"(?<=[.!?])\s", blurb, maxsplit=1)[0]   # outside the f-string: Python 3.11
    text = f"{job.get('company', '')} {first_sentence}".casefold()
    for tier in TIERS:
        for entry in profile["industries"][tier]:
            core = re.split(r"\s*\(", entry)[0].casefold()
            if re.search(rf"\b{re.escape(core)}\b", text):
                out.append(f"industries.{tier}: {entry!r} (named in the rationale's company sentence)")
    return out or ["base score only: no code adjustment, no tier named"]


def _cut(text, width):
    text = " ".join(str("" if text is None else text).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def rows(ranked, profile):
    from location import split_rationale
    out = []
    for i, job in enumerate(ranked, 1):
        blurb, _ = split_rationale(job.get("rationale"))
        out.append({
            "rank": i, "file": job["file"], "final": job.get("score", 0), "base": job.get("base_score", 0),
            "title": job["title"], "company": job["company"],
            "where": f"{job['location']} / {job.get('work_model') or '?'}",
            "adjustments": adjustments(job), "hard_block": job.get("hard_block_industry") or "",
            "rationale": blurb.splitlines()[0] if blurb else "",
            "drivers": drivers(job, profile),
        })
    return out


def calls_line(scored, cost=None):
    """How many Claude calls the run made and how many postings they scored
    (PUB-07b). The API call count includes the retry after a malformed reply,
    so it can exceed the postings; without usage (no cost summary) it is the
    postings scored."""
    api = cost["calls"] if cost is not None else scored
    retries = api - scored
    note = f", including {retries} {'retry' if retries == 1 else 'retries'} after a malformed reply" if retries > 0 else ""
    return f"Claude calls made: {api}{note}. Postings scored: {scored}."


def render(table, calls, cost=None):
    cols = (("#", "rank", 2), ("final", "final", 5), ("base", "base", 4), ("title", "title", 34),
            ("company", "company", 26), ("location / model", "where", 26), ("adjustments", "adjustments", 26),
            ("hard block", "hard_block", 10), ("rationale", "rationale", 70))
    lines = ["  ".join(f"{h:<{w}}" for h, _, w in cols)]
    lines.append("  ".join("-" * w for _, _, w in cols))
    for r in table:
        lines.append("  ".join(f"{_cut(r[k], w):<{w}}" for _, k, w in cols))
    lines += ["", "What drove the ranking:"]
    for r in table:
        lines.append(f"  {r['rank']:>2}. {r['company']} — {r['title']}")
        lines += [f"        {d}" for d in r["drivers"]]
    lines += ["", f"{calls_line(calls, cost)} Nothing was written (no Sheet, no seen_jobs.json, no queue)."]
    if cost is not None:
        lines += cost_lines(cost)
    return "\n".join(lines)


def _live(args):
    """calibrate --titles, or calibrate with no --dir: fetch live postings."""
    import profile_loader
    names, notes = pick_sources([n.strip() for n in args.sources.split(",") if n.strip()] if args.sources else None,
                                profile_loader.active()["sources"]["enabled"])
    if not args.titles and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY missing: calibrate scores each posting with one Claude call. Set it (or run "
              "calibrate --titles, which makes no model calls), then run again.")
        return 1
    log = sys.stderr if args.json else sys.stdout
    with contextlib.redirect_stdout(log):
        for note in notes:
            print(f"[Calibrate] {note}")
        with no_state_writes():
            got, skipped = fetch_live(names)
            for name, why in skipped.items():
                print(f"[Calibrate] {name} not run: {why}")
            passed, failed = gate(got)
            if not args.titles:
                picked = round_robin(passed, args.max)
                for i, job in enumerate(picked):
                    job["file"] = job.get("apply_link") or f"{job['source']}-{i}"
                print(f"[Calibrate] scoring {len(picked)} of {len(passed)} postings that passed "
                      f"(--max {args.max}, taken in turn from each source)")
                ranked, calls = score(picked)
    if args.titles:
        if args.json:
            keep = ("source", "title", "company", "location", "apply_link")
            print(json.dumps({"passed": [{k: j.get(k, "") for k in keep} for j in passed],
                              "failed": [{**{k: j.get(k, "") for k in keep}, "reason": j["calibrate_failed"]}
                                         for j in failed],
                              "not_run": skipped}, indent=2))
        else:
            print(render_titles(got, skipped, passed, failed, names, limit=args.max))
        return 0 if got else 1
    table = rows(ranked, profile_loader.active())
    for r, job in zip(table, ranked):
        r["source"] = job.get("source", "")
    cost = cost_summary(LAST_USAGE, per_day=len(passed))
    if args.json:
        print(json.dumps({"results": table, "claude_calls": cost["calls"], "postings_scored": calls, "cost": cost,
                          "not_run": skipped}, indent=2))
    else:
        print(render(table, calls, cost))
    return 0 if got else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python main.py calibrate",
                                     description="Rank postings under this profile: the samples in a folder, "
                                                 "or live postings from the sources (PUB-06, PUB-10)")
    parser.add_argument("--dir", help="score the .md/.txt postings in this folder (examples/postings has twelve)")
    parser.add_argument("--titles", action="store_true",
                        help="live postings through the title gate and country filter only: no model calls")
    parser.add_argument("--sources", help="comma-separated sources to fetch (default: the enabled fast ones, "
                                          f"{', '.join(FAST_SOURCES)}; slow: {', '.join(SLOW_SOURCES)})")
    parser.add_argument("--max", type=int, help=f"live: postings to score (default {DEFAULT_MAX}); "
                                                f"--titles: postings listed per list (default all)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)
    if args.dir and (args.titles or args.sources):
        parser.error("--dir scores a folder of postings; --titles and --sources fetch live ones")
    if not args.dir:
        if args.max is None and not args.titles:
            args.max = DEFAULT_MAX
        if args.max is not None and args.max < 1:
            parser.error("--max must be 1 or more")
        return _live(args)
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY missing: calibrate makes one Claude call per posting. Set it, then run again.")
        return 1
    if not os.path.isdir(args.dir) or not load_postings(args.dir):
        print(f"No .md or .txt postings in {args.dir}.")
        return 1
    import profile_loader
    # The scorer logs each call on stdout; --json keeps stdout for the JSON.
    with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
        ranked, calls = run(args.dir)
    table = rows(ranked, profile_loader.active())
    cost = cost_summary(LAST_USAGE)
    if args.json:
        print(json.dumps({"results": table, "claude_calls": cost["calls"], "postings_scored": calls, "cost": cost},
                         indent=2))
    else:
        print(render(table, calls, cost))
    return 0
