# resolve.py — `python main.py resolve`: find boards for the listed companies (PUB-11)
#
#   python main.py resolve                          # every list in the profile
#   python main.py resolve --list data/company_lists/my_companies.csv
#   python main.py resolve --max 200 --report-only  # probe, report, write nothing
#   python main.py resolve --yes                    # no question (the monthly workflow)
#
# PUB-12: every run first prints a preview -- companies to try per list and
# tier (after filters, minus those already tried and those a shipped list
# pre-resolved), the estimated wall time (config.RESOLVE_SECS_PER_COMPANY,
# measured in PUB-11), how many have a website and how many need the website
# ladder (websites.py), and what a Bespoke page costs on first read -- then
# asks "Continue? [y/N]" unless --yes. The dream tier goes first, then the
# lists in profile order; --max N stops after N companies and says how to go
# on (already-tried tracking makes the next run pick up where this one
# stopped). A vendor answering HTTP 429 is backed off exponentially, per
# vendor, up to a ceiling (config.RESOLVE_429_*); a company that still could
# not be probed is "deferred": counted per vendor, never written, never
# marked tried. A vendor that runs out of retries RESOLVE_429_CUTOFF requests
# in a row is cut off for the rest of the run: its requests answer 429 at once
# and its companies are deferred, so one rate-limited vendor cannot stall the
# batch (PUB-12: Workable held 500 companies for hours). Deferred companies
# are recorded in the pending file (never a resolution file, so never
# "tried") and go to the back of their list next run.
#
# Per company, in order (PUB-12): a known website's careers page first (the
# vendor read from its links, which is how a Workable tenant is found); the
# fast vendors' slug probes only when that finds no vendor. Workable's slug
# probe runs ONLY for a company with no known website, because Workable blocks
# address ranges rather than rate-limiting them: its own paced queue
# (WorkablePacer: one request per 10 s) holds no concurrency slot and gives up
# for the run after 5 consecutive 429s. It never holds a company past the
# batch: a company it could not probe is settled as "nothing found" with a
# note (WORKABLE_*_NOTE), which the shipped list records as outcome
# none/workable_unchecked.
#
# PUB-13: a guessed domain must also pass websites.title_names_company; one
# that fails is recorded as nothing found, "guessed domain, name not on page",
# and resolved no further. `--retry-unchecked` probes Workable for the companies
# a shipped list records as none/workable_unchecked and writes the answers back
# to that list and its boards file: board, or none; those not reached stay
# unchecked. `--dry-run` prints the counts and sends no request.
#
# A list row with an `outcome` (a shipped list the maintainer pre-resolved) is
# not resolved again, unless that outcome is `untried`.
#
# For each company in the profile's sources.company_lists that carries no
# board and has not been tried before, runs tools/resolve_ats.py's own steps
# (imported, never copied): the slug probes against every supported ATS API,
# then the careers-page pass, starting from the list's website when it has
# one. Each result goes to the file the engine already reads:
#
#   outcome           watch tier                          dream tier
#   board             sourcing/startups_ats_resolved.csv  data/companies.yaml companies:
#   workday tenant    sourcing/startups_ats_resolved.csv  companies.yaml pinned: workday
#   careers page      sourcing/startups_unresolved.csv    companies.yaml pinned: careers_pages
#                     (the Bespoke list)
#   unsupported vendor  sourcing/parked_boards.csv, "unsupported vendor: <vendor>"
#   nothing found       sourcing/parked_boards.csv, the resolver's reason
#
# "Tried before" means the name is already in any of those files. An
# unsupported vendor is one whose careers pages the engine cannot read
# (iCIMS, SuccessFactors, Phenom, ClearCompany, Pinpoint, and the others the
# resolver names), detected by the careers URL or the page; parking them
# keeps the gap countable. The report prints counts per outcome and per
# vendor and, when the watch-tier boards outgrow STARTUP_RUN_MAX_BOARDS, the
# shard planner's capacity note. Never touches a state file.

import argparse
import asyncio
import contextvars
import csv
import datetime
import os
import re
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))

# Careers-page hosts on vendors the engine has no parser for.
UNSUPPORTED_URL_PATTERNS = (
    ("icims", re.compile(r"(^|\.)icims\.com$")),
    ("successfactors", re.compile(r"(^|\.)(successfactors\.(com|eu)|sapsf\.(com|eu))$")),
    ("phenom", re.compile(r"(^|\.)phenompeople\.com$")),
    ("clearcompany", re.compile(r"(^|\.)clearcompany\.com$")),
    ("pinpoint", re.compile(r"(^|\.)pinpointhq\.com$")),
)
RESOLVED_FIELDS = ["company", "crunchbase_slug", "category", "hq", "market", "ats", "board_token",
                   "board_url", "method", "careers_url", "has_open_jobs", "website", "website_source"]
BESPOKE_FIELDS = ["company", "crunchbase_slug", "category", "hq", "market", "industries", "reason",
                  "careers_url", "rejected_board", "slug_candidates", "domains_tried", "website",
                  "website_source"]
OUTCOMES = ("board", "workday tenant", "careers page", "unsupported vendor", "nothing found", "deferred")
# Shipped-list outcomes that still leave the company to resolve.
UNSETTLED_OUTCOMES = ("", "untried")
# Why a company with no website has no Workable answer; tools/build_public_boards.py
# turns either into outcome none/workable_unchecked.
WORKABLE_BLOCKED_NOTE = "workable not probed (blocked)"
WORKABLE_BATCH_NOTE = "workable not probed (batch ended)"
GUESS_REJECTED_NOTE = "guessed domain, name not on page"       # websites.GUESS_REJECTED_NOTE
WORKABLE = "workable"
UNCHECKED = "none/workable_unchecked"
PENDING_FIELDS = ["company", "status", "vendors", "reason", "careers_url", "website", "website_source", "since"]
WORKDIR_FILES = {"resolved": "startups_ats_resolved.csv", "parked": "parked_boards.csv",
                 "bespoke": "startups_unresolved.csv", "companies": "companies.yaml",
                 "pending": "resolve_pending.csv"}
WEBSITE_CACHE = os.path.join(ROOT, ".website_cache")
# The vendors a company's probes were deferred on (HTTP 429 past the ceiling),
# set per company task; resolve_ats's own gathers copy the context, so they
# all add to the same set.
_DEFERRED = contextvars.ContextVar("resolve_deferred", default=None)
# True while a company is in its Workable phase: its Workable requests queue
# behind the careers-page ones (WorkablePacer).
_WORKABLE_PHASE = contextvars.ContextVar("resolve_workable_phase", default=False)

csv.field_size_limit(10 ** 9)


def _paths(workdir=None):
    if workdir:
        os.makedirs(workdir, exist_ok=True)
        return {k: os.path.join(workdir, f) for k, f in WORKDIR_FILES.items()}
    import config
    from sources import startups
    return {"resolved": str(startups.RESOLVED_CSV), "parked": str(startups.PARKED_CSV),
            "bespoke": config.BESPOKE_SOURCE_CSV, "companies": str(startups.COMPANIES_YAML),
            "pending": str(startups.RESOLVED_CSV.parent / "resolve_pending.csv")}


def unsupported_vendor(url):
    from urllib.parse import urlsplit
    host = (urlsplit(url or "").hostname or "").lower()
    return next((v for v, pattern in UNSUPPORTED_URL_PATTERNS if pattern.search(host)), None)


def _csv_names(path, column="company"):
    if not os.path.exists(path):
        return set()
    with open(path, newline="") as fh:
        return {(r.get(column) or "").strip().casefold() for r in csv.DictReader(fh)}


def tried_names(paths):
    """Every company name some resolution file already holds."""
    import yaml
    names = _csv_names(paths["resolved"]) | _csv_names(paths["bespoke"]) | _csv_names(paths["parked"])
    if os.path.exists(paths["companies"]):
        with open(paths["companies"]) as fh:
            data = yaml.safe_load(fh) or {}
        for key in ("companies", "unresolved"):
            names |= {str(e.get("name", "")).casefold() for e in data.get(key) or []}
        for section in (data.get("pinned") or {}).values():
            names |= {str(n).casefold() for n in (section or {})}
    return names - {""}


def _in_list(c, list_file):
    return not list_file or os.path.normpath(c.file) == os.path.normpath(list_file)


def needs_board(c):
    """A listed company the list does not resolve itself: no board of its own
    (ats + url) and no pre-resolved `outcome`."""
    return not (c.row.get("ats") and c.row.get("url")) and _unsettled(c.row)


def _unsettled(row):
    return (row.get("outcome") or "") in UNSETTLED_OUTCOMES


def to_resolve(companies, tried, list_file=None, deferred=()):
    """The companies to try, dream tier first, then the lists in profile order;
    within a list, companies deferred last run go last."""
    out = [c for c in companies if _in_list(c, list_file) and needs_board(c)
           and c.name.casefold() not in tried]
    return order(out, deferred)


def order(companies, deferred=()):
    files = list(dict.fromkeys(c.file for c in companies))
    return sorted(companies, key=lambda c: (c.tier != "dream", files.index(c.file),
                                            c.name.casefold() in deferred))


def pending_rows(paths):
    """{name casefold: row} of the pending file: companies a previous run deferred."""
    path = paths.get("pending")
    if not path or not os.path.exists(path):
        return {}
    with open(path, newline="") as fh:
        return {r["company"].casefold(): r for r in csv.DictReader(fh)
                if r.get("company") and r.get("status") == "deferred"}


def deferred_names(paths):
    """Companies a previous run deferred (HTTP 429), casefolded."""
    return set(pending_rows(paths))


def write_pending(paths, results, previous, today):
    """Keep the pending file to the companies still deferred: this run's added,
    anything this run settled dropped."""
    if not paths.get("pending"):
        return
    rows = dict(previous)
    for c, outcome, detail, _, report in results:
        key = c.name.casefold()
        if outcome == "deferred":
            old = rows.get(key, {})
            rows[key] = {"company": c.name, "status": outcome, "vendors": detail,
                         "reason": report.get("reason", ""), "careers_url": report.get("careers_url", ""),
                         "website": report.get("website", ""), "website_source": report.get("website_source", ""),
                         "since": old.get("since") or today}
        else:
            rows.pop(key, None)
    with open(paths["pending"], "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=PENDING_FIELDS, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows.values())


def plan(companies, tried, list_file=None):
    """[(file, tier, {to try, pre-resolved, already tried, carry boards})] per list, in run order."""
    groups = {}
    for c in companies:
        if not _in_list(c, list_file):
            continue
        g = groups.setdefault((c.file, c.tier), Counter())
        if c.row.get("ats") and c.row.get("url"):
            g["carry boards"] += 1
        elif not _unsettled(c.row):
            g["pre-resolved"] += 1
        elif c.name.casefold() in tried:
            g["already tried"] += 1
        else:
            g["to try"] += 1
    files = list(dict.fromkeys(f for f, _ in groups))
    return sorted(((f, t, n) for (f, t), n in groups.items()),
                  key=lambda g: (g[1] != "dream", files.index(g[0])))


def preview_lines(groups, todo, max_n=None, guess_websites=False):
    import config
    run = todo[:max_n] if max_n else todo
    lines = ["[Resolve] preview"]
    width = max([len(f) for f, _, _ in groups] + [4])
    lines.append(f"  {'list':<{width}}  tier   {'to try':>7}  {'pre-resolved':>12}  {'already tried':>13}")
    for f, tier, n in groups:
        lines.append(f"  {f:<{width}}  {tier:<5}  {n['to try']:>7}  {n['pre-resolved']:>12}  "
                     f"{n['already tried']:>13}")
    pre = sum(n["pre-resolved"] for _, _, n in groups)
    if pre:
        lines.append(f"  pre-resolved, nothing to do: {pre} (a shipped list already carries their outcome)")
    total = len(todo)
    lines.append(f"  to try: {total}" + (f"; this run stops after {len(run)} (--max {max_n})"
                                           if max_n and max_n < total else ""))
    with_site = sum(1 for c in run if c.website)
    laddered = sum(1 for c in run if not c.website and "website_source" in c.row)
    lines.append(f"  website: {with_site} have one in the list; {len(run) - with_site - laddered} need the "
                 f"website ladder (Wikidata by name, then domain guesses)"
                 + (f"; {laddered} have none after the list's own ladder" if laddered else ""))
    model = config.PROFILE_MODEL
    per_guess = config.model_cost(model, *config.WEBSITE_GUESS_TOKENS)
    most = len(run) - with_site - laddered
    guess_cost = None if per_guess is None else per_guess * most
    lines.append(f"  model guess (--guess-websites): {'on' if guess_websites else 'off'}; at most {most} "
                 f"companies would use it, about {config.format_cost(guess_cost)} at {model}")
    secs = len(run) * config.RESOLVE_SECS_PER_COMPANY
    lines.append(f"  estimated wall time: {_duration(secs)} ({config.RESOLVE_SECS_PER_COMPANY} s a company, "
                 f"measured in PUB-11; the website ladder adds to it)")
    page = config.model_cost(config.BESPOKE_EXTRACT_MODEL, *config.BESPOKE_FIRST_READ_TOKENS)
    lines.append(f"  Resolving is free; pages that end up in Bespoke cost about {config.format_cost(page)} "
                 f"each on first read")
    return lines


def _duration(secs):
    secs = int(round(secs))
    if secs < 90:
        return f"about {secs} s"
    hours, minutes = divmod(round(secs / 60), 60)
    return f"about {hours} h {minutes} min" if hours else f"about {minutes} min"


def classify(hit, report, deferred=()):
    """(outcome, detail) for one resolver result. A company with no hit whose
    probes a vendor kept refusing (HTTP 429) is deferred, not judged."""
    if hit:
        return ("workday tenant" if hit["ats"] == "workday" else "board"), hit["ats"]
    reason, url = report.get("reason", ""), report.get("careers_url", "")
    vendor = unsupported_vendor(url) or (reason.split(":", 1)[1] if reason.startswith("unsupported_ats:") else None)
    if vendor:
        return "unsupported vendor", vendor            # the page named its vendor: a refusal elsewhere changes nothing
    if deferred:
        return "deferred", ", ".join(sorted(deferred))
    if report.get("workable_note"):
        return "nothing found", report["workable_note"]
    if reason == "bespoke_page" and url:
        return "careers page", url
    return "nothing found", reason or "no_site"


def wants_workable(hit, report, website):
    """Workable's slug probe is only for a company with no known website that
    nothing settled. With a site, the careers page is what finds a Workable
    tenant, and a page or unsupported vendor found without one settles it too."""
    if hit or website:
        return False
    reason = report.get("reason", "")
    return (reason not in ("bespoke_page", GUESS_REJECTED_NOTE) and not reason.startswith("unsupported_ats:")
            and not unsupported_vendor(report.get("careers_url", "")))


async def _resolve_all(companies, resolver, ladder=None, concurrency=8, grace=None):
    """[(hit, report, deferred vendors, website, website_source)] per company.

    Phase one (the website ladder, then `resolver`) holds one of `concurrency`
    slots. A resolver with a `workable` coroutine adds phase two for the
    companies wants_workable picks: they wait on the Workable queue without a
    slot. Workable never holds a company past the batch: if it gives up
    (WorkableBlocked), or `grace` seconds after every phase one is done, the
    company comes back with report["workable_note"]."""
    import config
    grace = config.RESOLVE_WORKABLE_GRACE_SECS if grace is None else grace
    workable = getattr(resolver, "workable", None)
    sem = asyncio.Semaphore(concurrency)
    after_phase_one = {}
    left = [len(companies)]
    phase_one_done = asyncio.Event()

    async def phase_one(c):
        website, source = c.website, ("list" if c.website else "")
        if ladder is not None:
            if "website_source" in c.row:
                # A list that carries website_source went through the ladder when it
                # was built (tools/build_public_companies.py): its answer stands.
                source = c.row["website_source"] if c.website else ""
            else:
                website, source = await asyncio.to_thread(ladder.find, {**c.row, "name": c.name,
                                                                       "website": c.website})
                if source == "guess_rejected":
                    return None, {"reason": GUESS_REJECTED_NOTE}, "", ""
        hit, report = await resolver(c.name, website)
        return hit, report, website, source

    async def one(i, c):
        deferred = set()
        _DEFERRED.set(deferred)
        try:
            async with sem:
                hit, report, website, source = await phase_one(c)
        finally:
            left[0] -= 1
            if not left[0]:
                phase_one_done.set()
        after_phase_one[i] = (hit, report, deferred, website, source)
        if workable is not None and wants_workable(hit, report, website):
            _WORKABLE_PHASE.set(True)
            try:
                hit = await workable(c.name)
            except WorkableBlocked:
                report = {**report, "workable_note": WORKABLE_BLOCKED_NOTE}
        return hit, report, deferred, website, source

    tasks = [asyncio.create_task(one(i, c)) for i, c in enumerate(companies)]
    if not tasks:
        return []
    waiting_for_phase_one = asyncio.create_task(phase_one_done.wait())
    await asyncio.wait([waiting_for_phase_one, asyncio.gather(*tasks, return_exceptions=True)],
                       return_when=asyncio.FIRST_COMPLETED)
    waiting_for_phase_one.cancel()
    # Only the companies still running are waiting on Workable.
    still = {t for t in tasks if not t.done()}
    if still:
        _, still = await asyncio.wait(still, timeout=grace)
    for t in still:
        t.cancel()
    await asyncio.gather(*still, return_exceptions=True)
    out = []
    for i, t in enumerate(tasks):
        if t in still:
            hit, report, deferred, website, source = after_phase_one[i]
            out.append((None, {**report, "workable_note": WORKABLE_BATCH_NOTE}, deferred, website, source))
        else:
            out.append(t.result())                      # a phase-one error stops the run, as before
    return out


def vendor_of(host):
    """The vendor a host belongs to, as the 429 report names it: its registrable
    domain's first label (apply.workable.com -> workable)."""
    from tools.resolve_ats import throttle_key
    return throttle_key(host or "").split(".")[0] or host


class Backoff:
    """Per-vendor exponential backoff on HTTP 429. Each 429 in a row from a
    vendor doubles its wait (from base, never past ceiling) and holds every
    request to that vendor until the wait is over; any other answer resets it.
    A vendor whose requests run out of retries `cutoff` times in a row is cut
    off (`off`) for the rest of the run."""

    def __init__(self, base=None, ceiling=None, retries=None, sleep=None, clock=None, cutoff=None):
        import config
        self.base = config.RESOLVE_429_BASE_SECS if base is None else base
        self.ceiling = config.RESOLVE_429_CEILING_SECS if ceiling is None else ceiling
        self.retries = config.RESOLVE_429_RETRIES if retries is None else retries
        self.cutoff = config.RESOLVE_429_CUTOFF if cutoff is None else cutoff
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self.wait = {}
        self.until = {}
        self.hits = Counter()
        self.exhausted = Counter()
        self.off = set()

    async def pause(self, vendor):
        delay = self.until.get(vendor, 0.0) - self.clock()
        if delay > 0:
            await self.sleep(delay)

    def limited(self, vendor):
        self.hits[vendor] += 1
        wait = min(self.ceiling, self.wait.get(vendor, self.base / 2) * 2)
        self.wait[vendor] = wait
        self.until[vendor] = max(self.until.get(vendor, 0.0), self.clock() + wait)

    def ok(self, vendor):
        self.wait.pop(vendor, None)
        self.exhausted.pop(vendor, None)

    def give_up(self, vendor):
        """A request to `vendor` ran out of retries; cut the vendor off at `cutoff` in a row."""
        self.exhausted[vendor] += 1
        if self.exhausted[vendor] >= self.cutoff and vendor not in self.off:
            self.off.add(vendor)
            print(f"[Resolve] {vendor}: HTTP 429 past the ceiling {self.exhausted[vendor]} times in a row; "
                  f"cut off for the rest of this run, its companies are deferred", flush=True)


def _defer(vendor):
    deferred = _DEFERRED.get()
    if deferred is not None:
        deferred.add(vendor)


class WorkableBlocked(Exception):
    """Workable gave up for the run: `give_up` 429s in a row."""


class WorkablePacer:
    """Workable's own queue (PUB-12). One request at a time; the gap between
    requests starts at `start` seconds, doubles on each 429 up to `ceiling`,
    and shrinks by `recover` on each answered request, never below `start`.
    `give_up` 429s in a row set `blocked` for the rest of the run: every queued
    request is released at once and `acquire` answers False. Urgent requests (a
    careers page's Workable link, checked during a company's first phase) go
    ahead of the queued slug probes."""

    def __init__(self, start=None, ceiling=None, recover=None, sleep=None, clock=None, give_up=None):
        import config
        self.start = config.RESOLVE_WORKABLE_START_SECS if start is None else start
        self.ceiling = config.RESOLVE_WORKABLE_CEILING_SECS if ceiling is None else ceiling
        self.recover = config.RESOLVE_WORKABLE_RECOVER if recover is None else recover
        self.give_up = config.RESOLVE_WORKABLE_GIVE_UP if give_up is None else give_up
        self.refused = 0
        self.blocked = False
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self.gap = self.start
        self.widest = self.start
        self.next_ok = 0.0
        self.requests = 0
        self.limited_n = 0
        self._queue = []
        self._seq = 0
        self._busy = False
        self._cond = None

    async def acquire(self, urgent=False):
        import heapq
        if self._cond is None:
            self._cond = asyncio.Condition()
        cond = self._cond
        async with cond:
            self._seq += 1
            ticket = (0 if urgent else 1, self._seq)
            heapq.heappush(self._queue, ticket)
            try:
                while self._busy or self._queue[0] != ticket:
                    await cond.wait()
            except BaseException:
                self._queue.remove(ticket)
                heapq.heapify(self._queue)
                cond.notify_all()
                raise
            heapq.heappop(self._queue)
            if self.blocked:
                cond.notify_all()
                return False
            self._busy = True
        try:
            delay = self.next_ok - self.clock()
            if delay > 0:
                await self.sleep(delay)
            self.requests += 1
        finally:
            self.next_ok = max(self.next_ok, self.clock() + self.gap)
            async with cond:
                self._busy = False
                cond.notify_all()
        return True

    def limited(self):
        self.limited_n += 1
        self.refused += 1
        if self.refused >= self.give_up and not self.blocked:
            self.blocked = True
            print(f"[Resolve] workable: {self.refused} HTTP 429s in a row; giving up on it for this run",
                  flush=True)
        self.gap = min(self.ceiling, self.gap * 2)
        self.widest = max(self.widest, self.gap)
        self.next_ok = max(self.next_ok, self.clock() + self.gap)

    def ok(self):
        self.refused = 0
        self.gap = max(self.start, self.gap * self.recover)


def backoff_transport(inner, backoff, pacer=None):
    """An httpx async transport that retries a 429 under `backoff` and, past its
    retries, records the vendor on the current company (_DEFERRED). With a
    `pacer`, Workable requests go through it instead: queued and paced, until
    it gives up. Then a company's Workable phase raises WorkableBlocked and a
    careers-page check gets a 429 at once. Workable never defers a company."""
    import httpx

    class BackoffTransport(httpx.AsyncBaseTransport):
        async def _workable(self, request):
            phase = _WORKABLE_PHASE.get()
            attempt = 0
            while True:
                if pacer.blocked or not await pacer.acquire(urgent=not phase):
                    if phase:
                        raise WorkableBlocked()
                    return httpx.Response(429, request=request)
                response = await inner.handle_async_request(request)
                if response.status_code != 429:
                    pacer.ok()
                    return response
                pacer.limited()
                backoff.hits[WORKABLE] += 1
                attempt += 1
                if not phase and attempt > backoff.retries:
                    return response
                await response.aclose()

        async def handle_async_request(self, request):
            vendor = vendor_of(request.url.host)
            if pacer is not None and vendor == WORKABLE:
                return await self._workable(request)
            if vendor in backoff.off:
                _defer(vendor)
                return httpx.Response(429, request=request)
            for attempt in range(backoff.retries + 1):
                await backoff.pause(vendor)
                response = await inner.handle_async_request(request)
                if response.status_code != 429:
                    backoff.ok(vendor)
                    return response
                if attempt == backoff.retries:
                    _defer(vendor)
                    backoff.give_up(vendor)
                    return response
                await response.aclose()
                backoff.limited(vendor)
            return response

        async def aclose(self):
            await inner.aclose()
    return BackoffTransport()


async def workable_probe(client, name):
    """Workable's slug probes for one company, through the paced client: the
    first board that answers and passes the name check, else None."""
    import httpx

    from tools import resolve_ats as ra
    template, validator = ra.ATS_PROBES[WORKABLE]
    for candidate in ra.slug_candidates(name):
        url = template.format(slug=candidate.slug)
        response = None
        for _ in (1, 2):                                # one retry on a timeout
            try:
                response = await client.get(url)
                break
            except httpx.TimeoutException:
                continue
            except (httpx.HTTPError, httpx.InvalidURL):
                break
        if response is None or response.status_code != 200:
            continue
        try:
            data = response.json()
        except ValueError:
            continue
        if validator(data) and ra.slug_hit_accepted(name, WORKABLE, candidate, data):
            return {"name": name, "ats": WORKABLE, "slug": candidate.slug,
                    "resolved_at": datetime.date.today().isoformat(), "board_url": url,
                    "source": "workable_queue"}
    return None


def default_resolver(backoff=None, pacer=None):
    """An async (name, website) -> (hit, report) built from tools/resolve_ats,
    its HTTP behind the per-vendor 429 backoff: the careers page of a known
    website first, then the fast vendors' slug probes (every vendor but
    Workable). `resolve.workable(name)` is the Workable phase, on `pacer`, for
    the companies wants_workable picks."""
    import httpx

    from tools import resolve_ats as ra
    state = {}
    backoff = backoff or Backoff()
    pacer = pacer or WorkablePacer()
    fast = [ats for ats in ra.ATS_PROBES if ats != WORKABLE]
    # Workable is paced by `pacer`, so the per-host throttle leaves it alone.
    overrides = {k: v for k, v in ra.DOMAIN_DELAY_OVERRIDES.items() if not k.startswith(WORKABLE + ".")}

    def client():
        if "client" not in state:
            transport = backoff_transport(httpx.AsyncHTTPTransport(), backoff, pacer)
            state["client"] = httpx.AsyncClient(timeout=ra.REQUEST_TIMEOUT, follow_redirects=True,
                                                transport=transport,
                                                headers={"User-Agent": ra.USER_AGENT,
                                                         "Accept": "application/json"})
            state.update(throttle=ra.HostThrottle(overrides=overrides), sem=asyncio.Semaphore(ra.MAX_IN_FLIGHT),
                         web=ra.HostThrottle(base_delay=ra.WEBSITE_DELAY_SECONDS),
                         cache=ra.PageCache(ra.DEFAULT_CACHE_DIR), robots=ra.RobotsCache())
        return state["client"]

    async def resolve(name, website):
        client()
        s = state
        report = {}
        site = website if not website or "://" in website else f"https://{website}"
        try:
            hit = None
            if site:
                hit = await ra.resolve_via_careers(s["client"], name, s["web"], s["sem"], s["cache"],
                                                   s["robots"], seed_url=site, api_throttle=s["throttle"],
                                                   report=report, seed_only=True)
                if hit or report.get("reason", "").startswith("unsupported_ats:"):
                    return hit, report                  # the vendor came from the page itself
            hit = await ra.resolve_company(s["client"], name, s["throttle"], s["sem"], ats_names=fast)
            if not hit and not site:
                hit = await ra.resolve_via_careers(s["client"], name, s["web"], s["sem"], s["cache"],
                                                   s["robots"], api_throttle=s["throttle"], report=report)
        except Exception as e:                         # noqa: BLE001 -- one company never stops the run
            report["reason"] = f"error: {type(e).__name__}"
            hit = None
        return hit, report

    async def workable(name):
        try:
            return await workable_probe(client(), name)
        except (asyncio.CancelledError, WorkableBlocked):
            raise
        except Exception:                              # noqa: BLE001 -- one company never stops the run
            return None

    async def close():
        if "client" in state:
            await state["client"].aclose()
    resolve.close = close
    resolve.backoff = backoff
    resolve.pacer = pacer
    resolve.workable = workable
    return resolve


# --- Writing ------------------------------------------------------------------------------

def _append_csv(path, fields, rows):
    """Append rows under the file's own header. A field the rows carry that the
    header lacks (website, website_source on a file older than PUB-12) is
    added to the header, the file rewritten once."""
    exists = os.path.exists(path)
    if exists:
        with open(path, newline="") as fh:
            header = next(csv.reader(fh), fields)
        missing = [f for f in fields if f not in header and any(r.get(f) for r in rows)]
        if missing:
            with open(path, newline="") as fh:
                old = list(csv.DictReader(fh))
            header = header + missing
            with open(path, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
                writer.writeheader()
                writer.writerows(old)
        fields = header
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def write_results(results, paths, today):
    """Write each (company, outcome, detail, hit, report) where it belongs.
    Returns {file: rows written}."""
    from sources import startups
    resolved, bespoke, parked, written = [], [], [], Counter()
    data = None
    for c, outcome, detail, hit, report in results:
        site = {"website": report.get("website", ""), "website_source": report.get("website_source", "")}
        if outcome == "deferred":
            continue                                   # not written, not tried: the next run takes it
        if outcome in ("board", "workday tenant") and c.tier == "watch":
            resolved.append({"company": c.name, "ats": hit["ats"], "board_token": hit["slug"],
                             "board_url": hit["board_url"], "method": hit.get("source") or "slug",
                             "careers_url": hit.get("careers_url", ""),
                             "has_open_jobs": hit.get("has_open_jobs", ""), **site})
        elif outcome == "careers page" and c.tier == "watch":
            bespoke.append({"company": c.name, "reason": "bespoke_page", "careers_url": detail, **site})
        elif outcome in ("unsupported vendor", "nothing found"):
            url = report.get("careers_url", "") or c.website
            parked.append({"ats": detail if outcome == "unsupported vendor" else "none",
                           "board_token": c.name, "company": c.name, "board_url": url,
                           "reason": f"unsupported vendor: {detail}" if outcome == "unsupported vendor"
                           else f"resolve: {detail}",
                           "parked_on": today, "last_checked": today})
        else:                                          # the dream tier: companies.yaml
            if data is None:
                from tools import resolve_ats as ra
                data = ra.load_existing(_path(paths["companies"]))
                data["pinned"] = data["pinned"] or {}
            if outcome == "board":
                data["companies"].append({"name": c.name, "ats": hit["ats"], "slug": hit["slug"],
                                          "resolved_at": today, "board_url": hit["board_url"],
                                          **({"source": hit["source"]} if hit.get("source") else {}),
                                          **({k: v for k, v in site.items() if v})})
            elif outcome == "workday tenant":
                tenant, _, site = hit["slug"].partition(":")
                host = hit["board_url"].split("/")[0]
                data["pinned"].setdefault("workday", {})[c.name] = {"host": host, "tenant": tenant, "site": site}
            else:
                data["pinned"].setdefault("careers_pages", {})[c.name] = detail
            written[paths["companies"]] += 1
    if resolved:
        _append_csv(paths["resolved"], RESOLVED_FIELDS, resolved)
        written[paths["resolved"]] += len(resolved)
    if bespoke:
        _append_csv(paths["bespoke"], BESPOKE_FIELDS, bespoke)
        written[paths["bespoke"]] += len(bespoke)
    if parked:
        rows = {(r["ats"], r["board_token"]): r for r in _read_parked(paths["parked"])}
        for r in parked:
            rows.setdefault((r["ats"], r["board_token"]), r)
        _write_parked(paths["parked"], rows.values(), startups.PARKED_FIELDS)
        written[paths["parked"]] += len(parked)
    if data is not None:
        from tools import resolve_ats as ra
        ra.write_yaml(_path(paths["companies"]), data["companies"], data["unresolved"], data["pinned"])
    return written


def _path(p):
    from pathlib import Path
    return Path(p)


def _read_parked(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _write_parked(path, rows, fields):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for r in sorted(rows, key=lambda r: (r.get("ats", ""), r.get("board_token", ""))):
            writer.writerow({k: r.get(k, "") for k in fields})


# --- Report --------------------------------------------------------------------------------

def report_lines(results, total, skipped, written, capacity, backoff=None, pacer=None):
    by_outcome = Counter(o for _, o, _, _, _ in results)
    lines = [f"[Resolve] {total} listed companies with no board; {skipped} already tried, "
             f"{len(results)} resolved now"]
    for outcome in OUTCOMES:
        lines.append(f"  {outcome:<20}{by_outcome.get(outcome, 0):>6}")
    boards = Counter(d for _, o, d, _, _ in results if o in ("board", "workday tenant"))
    if boards:
        lines.append("  boards by vendor: " + ", ".join(f"{v} {n}" for v, n in boards.most_common()))
    found_by = Counter({"careers_page": "careers page", "workable_queue": "Workable queue"}.get(
        (h or {}).get("source"), "fast slug probes") for _, o, _, h, _ in results if o in ("board", "workday tenant"))
    if found_by:
        lines.append("  boards found by: " + ", ".join(f"{v} {n}" for v, n in found_by.most_common()))
    vendors = Counter(d for _, o, d, _, _ in results if o == "unsupported vendor")
    if vendors:
        lines.append("  unsupported vendors: " + ", ".join(f"{v} {n}" for v, n in vendors.most_common()))
    failures = Counter(d.split(":")[0] for _, o, d, _, _ in results if o == "nothing found")
    if failures:
        lines.append("  nothing found, by reason: " + ", ".join(f"{v} {n}" for v, n in failures.most_common()))
    deferred = Counter(v for _, o, d, _, _ in results if o == "deferred" for v in d.split(", "))
    if deferred:
        lines.append("  deferred (HTTP 429 past the backoff ceiling; not marked tried, the next run takes "
                     "them): " + ", ".join(f"{v} {n}" for v, n in deferred.most_common()))
    if backoff is not None and backoff.hits:
        lines.append("  HTTP 429 answers, by vendor: " + ", ".join(f"{v} {n}" for v, n in backoff.hits.most_common()))
    if pacer is not None and pacer.requests:
        lines.append(f"  Workable queue: {pacer.requests} requests, {pacer.limited_n} answered 429; gap now "
                     f"{pacer.gap:.1f} s (widest {pacer.widest:.1f} s)"
                     + ("; gave up for this run" if pacer.blocked else ""))
    if backoff is not None and backoff.off:
        lines.append("  cut off for this run (HTTP 429 past the ceiling "
                     f"{backoff.cutoff} times in a row): " + ", ".join(sorted(backoff.off)))
    sources = Counter(r.get("website_source") or "none" for _, _, _, _, r in results)
    if results:
        lines.append("  website from: " + ", ".join(f"{s} {sources.get(s, 0)}" for s in
                                                     ("list", "wikidata", "guess", "model", "none")))
    if written is None:
        lines.append("[Resolve] --report-only: nothing written")
    else:
        lines.append("[Resolve] wrote: " + (", ".join(f"{os.path.relpath(p, ROOT)} +{n}"
                                                      for p, n in written.items()) or "nothing"))
    lines += capacity
    return lines


def capacity_lines(results):
    """The shard planner's note for the watch-tier boards after this run (the
    new boards included, written or not), when they exceed the run cap."""
    from config import STARTUP_RUN_MAX_BOARDS
    from sources import startups
    boards = startups.load_boards()
    keys = {(b["ats"], b["slug"]) for b in boards}
    for c, outcome, _, hit, _ in results:
        if outcome in ("board", "workday tenant") and c.tier == "watch" and (hit["ats"], hit["slug"]) not in keys:
            boards.append({"company": c.name, "ats": hit["ats"], "slug": hit["slug"], "board_url": hit["board_url"]})
    two_day, largest = startups.plan_cadence(boards)
    if not two_day and largest <= STARTUP_RUN_MAX_BOARDS:
        return [f"[Resolve] board tier: {len(boards)} boards, largest run {largest} of "
                f"{STARTUP_RUN_MAX_BOARDS}: within capacity"]
    return startups.cadence_note(two_day, largest)


def default_ladder(guess_websites=False, log=print):
    """The website ladder resolve uses: Wikidata by name (cached under
    .website_cache/), domain guesses, and the model rung only when asked."""
    import config
    import websites
    contact = os.environ.get("GMAIL_ADDRESS", "").strip()
    http = websites.Http(f"job-digest-agent website ladder{f' ({contact})' if contact else ''}")
    wikidata = websites.Wikidata(http.json, os.path.join(WEBSITE_CACHE, "wikidata"))
    guesser = None
    if guess_websites:
        guesser = websites.ModelGuesser(config.PROFILE_MODEL,
                                            os.path.join(WEBSITE_CACHE, "model"))
    return websites.Ladder(http, wikidata, guesser, log=log)


def confirm(yes, input_fn=None, log=print):
    if yes:
        return True
    if input_fn is None:
        if not sys.stdin.isatty():
            log("[Resolve] not run: no terminal to answer \"Continue?\"; pass --yes to run without the question")
            return False
        input_fn = input
    answer = (input_fn("Continue? [y/N] ") or "").strip().lower()
    if answer not in ("y", "yes"):
        log("[Resolve] stopped; nothing resolved")
        return False
    return True


def run(list_file=None, max_n=None, report_only=False, resolver=None, paths=None, today=None, log=print,
        yes=False, input_fn=None, ladder=None, guess_websites=False, workdir=None):
    import company_lists
    import profile_loader
    entries = profile_loader.active()["sources"]["company_lists"]
    if list_file and not any(os.path.normpath(e["file"]) == os.path.normpath(list_file) for e in entries):
        log(f"[Resolve] {list_file} is not in the profile's sources.company_lists; add it there first.")
        return 1
    paths = paths or _paths(workdir)
    companies = company_lists.load(entries)
    tried = tried_names(paths)
    needing = [c for c in companies if _in_list(c, list_file) and needs_board(c)]
    previous = pending_rows(paths)
    todo_all = to_resolve(companies, tried, list_file, set(previous))
    skipped = len(needing) - len(todo_all)
    for line in preview_lines(plan(companies, tried, list_file), todo_all, max_n, guess_websites):
        log(line)
    if not todo_all:
        log("[Resolve] nothing to resolve")
        return 0
    if not confirm(yes, input_fn, log):
        return 0
    todo = todo_all[:max_n] if max_n else todo_all
    log(f"[Resolve] resolving {len(todo)} companies{' (report only)' if report_only else ''}")
    own = resolver is None
    resolver = resolver or default_resolver()
    if ladder is None and own:
        ladder = default_ladder(guess_websites, log)
    started = time.monotonic()
    answers = asyncio.run(_run_all(todo, resolver, own, ladder))
    results = []
    for c, (hit, report, deferred, website, source) in zip(todo, answers):
        report = {**report, "website": website or "", "website_source": source or ""}
        outcome, detail = classify(hit, report, deferred)
        results.append((c, outcome, detail, hit, report))
    today = today or datetime.date.today().isoformat()
    written = None if report_only else write_results(results, paths, today)
    if not report_only:
        write_pending(paths, results, previous, today)
    capacity = [] if workdir else capacity_lines(results)
    for line in report_lines(results, len(needing), skipped, written, capacity,
                             getattr(resolver, "backoff", None), getattr(resolver, "pacer", None)):
        log(line)
    guesser = getattr(ladder, "guesser", None)
    if guesser is not None:
        import config
        log(f"[Resolve] model website guesses: {guesser.calls} calls, {guesser.input_tokens} input and "
            f"{guesser.output_tokens} output tokens, {config.format_cost(guesser.cost())}")
    log(f"[Resolve] wall time {_duration(time.monotonic() - started)}")
    deferred_n = sum(1 for _, o, _, _, _ in results if o == "deferred")
    left = len(todo_all) - len(todo) + deferred_n
    if left:
        log(f"[Resolve] {left} companies left to try ({deferred_n} deferred); run the same command again "
            f"to continue -- companies already tried are skipped")
    return 0


def _retry_entries(entries, list_file):
    """The list entries --retry-unchecked reads. --list need not be in the
    profile: the shipped list holds the marker whether a profile uses it or not."""
    if not list_file:
        return list(entries)
    chosen = [e for e in entries if os.path.normpath(e["file"]) == os.path.normpath(list_file)]
    return chosen or [{"file": list_file, "tier": "watch"}]


async def _retry_all(todo, probe, close=None):
    """(found [(company, hit)], nothing found [company], not reached [company]).
    The Workable give-up rule ends the run: what is left stays unchecked."""
    _WORKABLE_PHASE.set(True)
    found, nothing = [], []
    try:
        for i, c in enumerate(todo):
            try:
                hit = await probe(c.name)
            except WorkableBlocked:
                return found, nothing, todo[i:]
            if hit:
                found.append((c, hit))
            else:
                nothing.append(c)
        return found, nothing, []
    finally:
        if close:
            await close()


def write_retry(found, nothing, entries):
    """Write the answers back: `outcome` in each company's list, new boards to
    the list's `boards:` file (starter_boards.csv beside it when it names none)."""
    import company_lists
    from tools import build_public_boards
    boards_file = {e["file"]: e.get("boards") for e in entries}
    for file in {c.file for c in [c for c, _ in found] + nothing}:
        path = company_lists._full(file)
        answers = {c.name.casefold(): "none" for c in nothing if c.file == file}
        answers.update({c.name.casefold(): "board" for c, _ in found if c.file == file})
        build_public_boards.update_list(answers, {}, path=path)
        rows = [{"name": c.name, "ats": h["ats"], "url": h["board_url"], "website": c.website}
                for c, h in found if c.file == file]
        if rows:
            target = company_lists._full(boards_file.get(file) or os.path.join(os.path.dirname(file),
                                                                               "starter_boards.csv"))
            _append_csv(target, build_public_boards.HEADER, rows)


def run_retry(list_file=None, max_n=None, report_only=False, dry_run=False, yes=False, input_fn=None,
              probe=None, log=print):
    """`resolve --retry-unchecked`: Workable's paced slug probe for the companies
    marked none/workable_unchecked, and nothing else."""
    import company_lists
    import config
    import profile_loader
    from tools import resolve_ats as ra
    entries = _retry_entries(profile_loader.active()["sources"]["company_lists"], list_file)
    unchecked = [c for c in company_lists.load(entries) if c.row.get("outcome") == UNCHECKED]
    todo = unchecked[:max_n] if max_n else unchecked
    requests = sum(len(ra.slug_candidates(c.name)) for c in todo)
    log(f"[Retry] {len(unchecked)} companies are {UNCHECKED}; this run probes {len(todo)}"
        + (f" (--max {max_n})" if max_n and max_n < len(unchecked) else ""))
    log(f"[Retry] about {requests} Workable requests, one per {config.RESOLVE_WORKABLE_START_SECS} s at best: "
        f"{_duration(requests * config.RESOLVE_WORKABLE_START_SECS)}")
    if dry_run:
        log("[Retry] --dry-run: nothing probed, nothing written")
        return 0
    if not todo:
        log("[Retry] nothing to retry")
        return 0
    if not confirm(yes, input_fn, log):
        return 0
    resolver = None
    if probe is None:
        resolver = default_resolver()
        probe = resolver.workable
    found, nothing, left = asyncio.run(_retry_all(todo, probe, resolver.close if resolver else None))
    if not report_only:
        write_retry(found, nothing, entries)
    log(f"[Retry] probed {len(found) + len(nothing)}: {len(found)} boards found, {len(nothing)} nothing found, "
        f"{len(left)} not reached (still unchecked)")
    if resolver is not None and resolver.pacer.requests:
        log(f"[Retry] Workable queue: {resolver.pacer.requests} requests, {resolver.pacer.limited_n} answered 429"
            + ("; gave up for this run" if resolver.pacer.blocked else ""))
    log("[Retry] --report-only: nothing written" if report_only else
        f"[Retry] {len(unchecked) - len(found) - len(nothing)} companies remain {UNCHECKED}")
    return 0


async def _run_all(todo, resolver, own, ladder=None):
    try:
        return await _resolve_all(todo, resolver, ladder)
    finally:
        if own and hasattr(resolver, "close"):
            await resolver.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python main.py resolve",
                                     description="Find boards for the companies in the profile's lists (PUB-11)")
    parser.add_argument("--list", dest="list_file", help="only this list file (one of sources.company_lists)")
    parser.add_argument("--max", dest="max_n", type=int, help="resolve at most this many companies")
    parser.add_argument("--report-only", action="store_true", help="probe and report; write nothing")
    parser.add_argument("--yes", action="store_true", help="skip the Continue? question (the monthly workflow)")
    parser.add_argument("--guess-websites", action="store_true",
                        help="last rung of the website ladder: ask the utility model (one call per company "
                             "still without a website; the preview prices it)")
    parser.add_argument("--workdir", help="write the resolution files here instead of the engine's own "
                                          "(the maintainer's pre-resolve of a shipped list)")
    parser.add_argument("--retry-unchecked", action="store_true",
                        help=f"only the companies marked {UNCHECKED}: probe Workable, paced (--list need not be "
                             "in the profile)")
    parser.add_argument("--dry-run", action="store_true", help="with --retry-unchecked: print the counts only")
    args = parser.parse_args(argv)
    if args.max_n is not None and args.max_n < 1:
        parser.error("--max must be 1 or more")
    if args.dry_run and not args.retry_unchecked:
        parser.error("--dry-run goes with --retry-unchecked")
    if args.retry_unchecked:
        return run_retry(args.list_file, args.max_n, args.report_only, args.dry_run, args.yes)
    return run(args.list_file, args.max_n, args.report_only, yes=args.yes,
               guess_websites=args.guess_websites, workdir=args.workdir)
