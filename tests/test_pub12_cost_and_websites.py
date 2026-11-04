"""PUB-12: resolve's preview, batching and 429 backoff; the per-run scoring cap;
the website ladder; cost reporting; roles.rationale_example; the shipped
public boards. No network access: HTTP, Wikidata and the model are stubbed."""

import asyncio
import csv
import json
import types
from pathlib import Path

import httpx
import pytest

import company_lists
import config
import profile_loader
import resolve
import websites

ROOT = Path(__file__).resolve().parent.parent
LISTS = ROOT / "data" / "company_lists"


def _write(path, text):
    path.write_text(text)
    return str(path)


def _profile_with(monkeypatch, entries=None, **scoring):
    profile = profile_loader.with_defaults(profile_loader.active())
    if entries is not None:
        profile["sources"]["company_lists"] = entries
    profile["scoring"].update(scoring)
    monkeypatch.setattr(profile_loader, "active", lambda: profile_loader._freeze(profile))
    return profile


# --- STEP 1: preview, order, batching, resume -----------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    dream = _write(tmp_path / "dream.csv", "name,website,notes\nDream One,dreamone.com,\n")
    watch = _write(tmp_path / "watch.csv", "name,website,notes,outcome\nA Co,aco.com,,\nB Co,,,\nC Co,,,\n"
                   "D Co,,,\nE Co,,,\nDone Co,,,board\nGone Co,,,none\nOld Co,,,\n")
    _profile_with(monkeypatch, [{"file": watch, "tier": "watch"}, {"file": dream, "tier": "dream"}])
    paths = {k: str(tmp_path / f) for k, f in resolve.WORKDIR_FILES.items()}
    Path(paths["parked"]).write_text("ats,board_token,company,board_url,reason,parked_on,last_checked\n"
                                     "none,Old Co,Old Co,,resolve: no_site,2026-10-01,2026-10-01\n")
    asked = []

    async def resolver(name, website):
        asked.append(name)
        return None, {"reason": "no_site"}
    return paths, resolver, asked


def test_preview_counts_lists_tiers_pre_resolved_and_wall_time(env, capsys):
    paths, resolver, asked = env
    assert resolve.run(resolver=resolver, paths=paths, input_fn=lambda _: "n") == 0
    out = capsys.readouterr().out
    assert asked == []                                         # "n": nothing resolved
    assert "[Resolve] stopped; nothing resolved" in out
    lines = out.splitlines()
    dream = next(line for line in lines if "dream.csv" in line)
    watch = next(line for line in lines if "watch.csv" in line)
    assert lines.index(dream) < lines.index(watch)              # dream tier first
    assert dream.split()[-4:] == ["dream", "1", "0", "0"]
    assert watch.split()[-4:] == ["watch", "5", "2", "1"]       # 5 to try, 2 pre-resolved, Old Co tried
    assert "pre-resolved, nothing to do: 2" in out
    assert "  to try: 6" in out
    assert "website: 2 have one in the list; 4 need the website ladder" in out
    assert f"estimated wall time: about {round(6 * config.RESOLVE_SECS_PER_COMPANY)} s" in out
    page = config.model_cost(config.BESPOKE_EXTRACT_MODEL, *config.BESPOKE_FIRST_READ_TOKENS)
    assert f"Resolving is free; pages that end up in Bespoke cost about {config.format_cost(page)} each" in out
    assert "model guess (--guess-websites): off; at most 4 companies" in out


def test_no_terminal_and_no_yes_resolves_nothing(env, capsys, monkeypatch):
    paths, resolver, asked = env
    monkeypatch.setattr(resolve.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert resolve.run(resolver=resolver, paths=paths) == 0
    assert asked == [] and "pass --yes" in capsys.readouterr().out


def test_max_stops_after_n_and_the_next_run_picks_up(env, capsys):
    paths, resolver, asked = env
    assert resolve.run(resolver=resolver, paths=paths, max_n=4, yes=True, today="2026-10-29") == 0
    assert asked == ["Dream One", "A Co", "B Co", "C Co"]
    out = capsys.readouterr().out
    assert "this run stops after 4 (--max 4)" in out
    assert "2 companies left to try (0 deferred); run the same command again" in out
    asked.clear()
    assert resolve.run(resolver=resolver, paths=paths, max_n=4, yes=True, today="2026-10-29") == 0
    assert asked == ["D Co", "E Co"]                            # resumes; pre-resolved never asked
    asked.clear()
    resolve.run(resolver=resolver, paths=paths, yes=True)
    assert asked == [] and "nothing to resolve" in capsys.readouterr().out


# --- STEP 1: per-vendor backoff and deferred counts ---------------------------------------

def test_backoff_doubles_to_the_ceiling_and_resets():
    b = resolve.Backoff(base=2, ceiling=10, retries=4, clock=lambda: 0.0)
    for want in (2, 4, 8, 10, 10):
        b.limited("workable")
        assert b.wait["workable"] == want
    b.ok("workable")
    b.limited("workable")
    assert b.wait["workable"] == 2


def _client(handler, backoff):
    return httpx.AsyncClient(transport=resolve.backoff_transport(httpx.MockTransport(handler), backoff))


def test_a_vendor_429_past_the_retries_defers_the_company_without_marking_it_tried(env, capsys):
    paths, _, _ = env
    slept = []

    async def sleep(s):
        slept.append(s)
    backoff = resolve.Backoff(base=2, ceiling=8, retries=3, sleep=sleep, clock=lambda: 0.0)
    calls = {"workable": 0, "greenhouse": 0}

    def handler(request):
        vendor = "workable" if "workable" in request.url.host else "greenhouse"
        calls[vendor] += 1
        return httpx.Response(429 if vendor == "workable" else 404)

    async def resolver(name, website):
        async with _client(handler, backoff) as client:
            await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{name.split()[0].lower()}")
            await client.get("https://boards-api.greenhouse.io/v1/boards/x/jobs")
        return None, {"reason": "no_site"}
    resolver.backoff = backoff
    assert resolve.run(resolver=resolver, paths=paths, yes=True, max_n=2) == 0
    assert calls == {"workable": 8, "greenhouse": 2}              # 1 + 3 retries each, greenhouse untouched
    out = capsys.readouterr().out
    assert "  deferred                 2" in out
    assert "deferred (HTTP 429 past the backoff ceiling; not marked tried, the next run takes them): workable 2" in out
    assert "HTTP 429 answers, by vendor: workable 6" in out
    assert "Dream One" not in Path(paths["parked"]).read_text()   # not written, so not tried
    assert resolve.to_resolve(company_lists.load(profile_loader.active()["sources"]["company_lists"]),
                              resolve.tried_names(paths))[0].name == "Dream One"


def test_a_vendor_that_keeps_refusing_is_cut_off_for_the_rest_of_the_run(env, capsys):
    paths, _, _ = env
    backoff = resolve.Backoff(base=1, ceiling=2, retries=1, cutoff=2,
                              sleep=lambda s: asyncio.sleep(0), clock=lambda: 0.0)
    calls = {"workable": 0, "greenhouse": 0}

    def handler(request):
        vendor = "workable" if "workable" in request.url.host else "greenhouse"
        calls[vendor] += 1
        return httpx.Response(429 if vendor == "workable" else 404)

    async def resolver(name, website):
        async with _client(handler, backoff) as client:
            await client.get(f"https://apply.workable.com/api/v1/widget/accounts/{name.split()[0].lower()}")
            await client.get("https://boards-api.greenhouse.io/v1/boards/x/jobs")
        return None, {"reason": "no_site"}
    resolver.backoff = backoff
    assert resolve.run(resolver=resolver, paths=paths, yes=True, max_n=3) == 0
    assert backoff.off == {"workable"}
    before = calls["workable"]

    async def after_cutoff():
        deferred = set()
        resolve._DEFERRED.set(deferred)
        async with _client(handler, backoff) as client:
            r = await client.get("https://apply.workable.com/api/v1/widget/accounts/late")
        return r.status_code, deferred
    assert asyncio.run(after_cutoff()) == (429, {"workable"})
    assert calls["workable"] == before                            # answered at once, no request sent
    out = capsys.readouterr().out
    assert "workable: HTTP 429 past the ceiling 2 times in a row; cut off" in out
    assert "  deferred                 3" in out                   # the third is deferred without a request
    assert "cut off for this run (HTTP 429 past the ceiling 2 times in a row): workable" in out


def test_an_answer_between_refusals_resets_the_cutoff_count():
    b = resolve.Backoff(cutoff=2, clock=lambda: 0.0)
    b.give_up("workable")
    b.ok("workable")
    b.give_up("workable")
    assert b.off == set()
    b.give_up("workable")
    assert b.off == {"workable"}


def test_deferred_companies_go_to_the_back_next_run_and_leave_the_file_once_settled(env):
    paths, _, _ = env
    asked, refusing = [], {"A Co"}

    async def resolver(name, website):
        asked.append(name)
        if name in refusing:
            resolve._DEFERRED.get().add("workable")
        return None, {"reason": "no_site"}
    assert resolve.run(resolver=resolver, paths=paths, yes=True, max_n=2) == 0
    assert asked == ["Dream One", "A Co"]
    assert resolve.deferred_names(paths) == {"a co"}
    assert "A Co,deferred,workable," in Path(paths["pending"]).read_text()

    asked.clear()
    assert resolve.run(resolver=resolver, paths=paths, yes=True, max_n=2) == 0
    assert asked == ["B Co", "C Co"]                              # A Co waits at the back
    assert resolve.deferred_names(paths) == {"a co"}

    asked.clear()
    refusing.clear()
    assert resolve.run(resolver=resolver, paths=paths, yes=True) == 0
    assert asked == ["D Co", "E Co", "A Co"]
    assert resolve.deferred_names(paths) == set()                 # settled, so dropped


def test_workable_is_paced_like_the_other_tenant_wide_rate_limiters():
    from tools import resolve_ats as ra
    assert ra.DOMAIN_DELAY_OVERRIDES["workable.com"] >= 2.0
    assert ra.throttle_key("apply.workable.com") == "workable.com"


# --- The Workable queue (PUB-12 follow-up) -------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, s):
        self.now += s
        await asyncio.sleep(0)


def test_workable_pacer_backs_off_on_429_and_recovers_slowly():
    clock = _Clock()
    pacer = resolve.WorkablePacer(start=3, ceiling=24, recover=0.5, sleep=clock.sleep, clock=clock, give_up=99)

    async def go():
        times = []
        for _ in range(3):
            await pacer.acquire()
            times.append(clock.now)
        return times
    assert asyncio.run(go()) == [0.0, 3.0, 6.0]
    pacer.limited()
    pacer.limited()
    assert pacer.gap == 12 and pacer.widest == 12
    for _ in range(5):
        pacer.limited()
    assert pacer.gap == 24                                        # never past the ceiling
    pacer.ok()
    assert pacer.gap == 12
    for _ in range(5):
        pacer.ok()
    assert pacer.gap == 3                                         # never below the start


def test_an_urgent_workable_request_goes_ahead_of_queued_slug_probes():
    clock = _Clock()
    pacer = resolve.WorkablePacer(start=3, sleep=clock.sleep, clock=clock)
    order = []

    async def take(label, urgent):
        await pacer.acquire(urgent=urgent)
        order.append(label)

    async def go():
        await pacer.acquire()
        await asyncio.gather(take("slug 1", False), take("slug 2", False), take("careers", True))
    asyncio.run(go())
    # slug 1 already holds its turn (waiting out the gap); the careers request takes the next
    # one, ahead of slug 2 which queued before it.
    assert order == ["slug 1", "careers", "slug 2"]


def test_in_its_workable_phase_a_429_is_retried_until_answered_not_deferred():
    clock = _Clock()
    pacer = resolve.WorkablePacer(start=1, ceiling=8, sleep=clock.sleep, clock=clock)
    backoff = resolve.Backoff(retries=1, clock=clock)
    answers = iter([429, 429, 429, 200])

    async def go():
        deferred = set()
        resolve._DEFERRED.set(deferred)
        resolve._WORKABLE_PHASE.set(True)
        transport = resolve.backoff_transport(httpx.MockTransport(lambda r: httpx.Response(next(answers))),
                                              backoff, pacer)
        async with httpx.AsyncClient(transport=transport) as client:
            r = await client.get("https://apply.workable.com/api/v1/widget/accounts/x")
        return r.status_code, deferred
    assert asyncio.run(go()) == (200, set())
    assert pacer.limited_n == 3 and pacer.requests == 4 and backoff.off == set()


def test_workable_starts_at_one_request_per_10_s_and_gives_up_after_5():
    assert config.RESOLVE_WORKABLE_START_SECS == 10 and config.RESOLVE_WORKABLE_GIVE_UP == 5
    pacer = resolve.WorkablePacer(clock=lambda: 0.0)
    for _ in range(4):
        pacer.limited()
    pacer.ok()                                                    # an answer resets the count
    for _ in range(4):
        pacer.limited()
    assert not pacer.blocked
    pacer.limited()
    assert pacer.blocked


def test_a_blocked_workable_releases_the_queue_and_the_phase_raises():
    clock = _Clock()
    pacer = resolve.WorkablePacer(start=1, give_up=2, sleep=clock.sleep, clock=clock)
    backoff = resolve.Backoff(clock=clock)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429)

    async def get(client, name):
        resolve._WORKABLE_PHASE.set(True)
        await client.get(f"https://apply.workable.com/{name}")

    async def go():
        transport = resolve.backoff_transport(httpx.MockTransport(handler), backoff, pacer)
        async with httpx.AsyncClient(transport=transport) as client:
            phase = await asyncio.gather(*(get(client, n) for n in "abcd"), return_exceptions=True)
            careers = await client.get("https://apply.workable.com/token")   # a careers-page check, after the block
        return phase, careers.status_code
    phase, careers = asyncio.run(go())
    assert all(isinstance(e, resolve.WorkableBlocked) for e in phase)
    assert len(calls) == 2 and pacer.blocked                      # nothing sent past the second 429
    assert careers == 429                                         # answered at once; not deferred


def _two_phase_resolver(workable_answers, phase_one_hits=()):
    calls = {"phase one": [], "workable": []}

    async def resolver(name, website):
        calls["phase one"].append(name)
        await asyncio.sleep(0)
        if name in phase_one_hits:
            return {"ats": "greenhouse", "slug": "x", "board_url": "https://boards-api.greenhouse.io/v1/boards/x/jobs"}, {}
        return None, {"reason": "no_site"}

    async def workable(name):
        calls["workable"].append(name)
        answer = workable_answers.get(name, "never")
        if answer == "never":
            await asyncio.Event().wait()                          # still queued when the batch ends
        if answer == "blocked":
            raise resolve.WorkableBlocked()
        return answer
    resolver.workable = workable
    return resolver, calls


def _companies(*names, website=""):
    return [company_lists.Company(name=n, website=website, notes="", tier="watch", file="w.csv", row={})
            for n in names]


def test_a_company_with_a_website_is_never_slug_probed_on_workable():
    resolver, calls = _two_phase_resolver({})
    out = asyncio.run(resolve._resolve_all(_companies("A Co", website="https://aco.com"), resolver, grace=5))
    assert calls["workable"] == []
    assert resolve.classify(*out[0][:3]) == ("nothing found", "no_site")


def test_the_workable_probe_runs_for_no_website_and_holds_no_slot():
    hit = {"ats": "workable", "slug": "cco", "board_url": "u", "source": "workable_queue"}
    resolver, calls = _two_phase_resolver({"C Co": hit}, phase_one_hits={"B Co"})
    out = asyncio.run(resolve._resolve_all(_companies("A Co", "B Co", "C Co"), resolver, concurrency=1, grace=0.05))
    assert calls["phase one"] == ["A Co", "B Co", "C Co"]       # A waiting on Workable never held the slot
    assert calls["workable"] == ["A Co", "C Co"]                 # B settled on its fast vendor
    assert [resolve.classify(*o[:3]) for o in out] == [("nothing found", resolve.WORKABLE_BATCH_NOTE),
                                                       ("board", "greenhouse"), ("board", "workable")]


def test_a_vendor_named_on_the_page_settles_a_company_another_vendor_refused():
    assert resolve.classify(None, {"reason": "unsupported_ats:jobvite"}, {"personio"}) == ("unsupported vendor", "jobvite")
    assert resolve.classify(None, {"reason": "no_site"}, {"personio"}) == ("deferred", "personio")


def test_a_page_found_without_a_website_needs_no_workable_probe():
    assert not resolve.wants_workable(None, {"reason": "bespoke_page", "careers_url": "https://x.com/c"}, "")
    assert not resolve.wants_workable(None, {"reason": "unsupported_ats:icims"}, "")
    assert resolve.wants_workable(None, {"reason": "no_site"}, "")
    assert resolve.wants_workable(None, {"reason": "content_check_failed"}, "")


def test_blocked_workable_settles_the_batch_as_none_with_the_note_and_marks_them_tried(env, capsys):
    paths, _, _ = env
    resolver, calls = _two_phase_resolver({"B Co": "blocked", "C Co": "blocked"})
    assert resolve.run(resolver=resolver, paths=paths, yes=True, max_n=4) == 0   # B and C have no website
    out = capsys.readouterr().out
    assert calls["workable"][:2] == ["B Co", "C Co"]
    assert "  deferred                 0" in out
    parked = Path(paths["parked"]).read_text()
    assert "resolve: workable not probed (blocked)" in parked
    assert {"b co", "c co"} <= resolve.tried_names(paths)         # settled: not held for the next run
    assert resolve.pending_rows(paths) == {}


def test_default_resolver_reads_a_known_website_first_and_never_slug_probes_workable_there(monkeypatch):
    from tools import resolve_ats as ra
    seen = []

    async def careers(client, name, *a, seed_url=None, report=None, seed_only=False, **k):
        seen.append(("careers", seed_url, seed_only))
        if name == "Linked Co":
            return {"ats": "lever", "slug": "linked", "board_url": "u", "source": "careers_page"}
        if name == "Odd Co":
            report["reason"] = "unsupported_ats:icims"
        else:
            report["reason"] = "bespoke_page"
        return None

    async def slugs(client, name, throttle, sem, ats_names=None):
        seen.append(("slugs", name, tuple(ats_names)))
        return None
    monkeypatch.setattr(ra, "resolve_via_careers", careers)
    monkeypatch.setattr(ra, "resolve_company", slugs)
    resolver = resolve.default_resolver()

    async def go():
        try:
            return [await resolver(n, w) for n, w in
                    (("Linked Co", "linked.com"), ("Odd Co", "odd.com"), ("Plain Co", "plain.com"))]
        finally:
            await resolver.close()
    hits = asyncio.run(go())
    assert hits[0][0]["ats"] == "lever"
    assert seen[0] == ("careers", "https://linked.com", True)
    assert ("slugs", "Linked Co") not in [s[:2] for s in seen]    # the page named the vendor
    assert ("slugs", "Odd Co") not in [s[:2] for s in seen]       # an unsupported vendor settles it too
    plain = [s for s in seen if s[0] == "slugs"]
    assert [s[1] for s in plain] == ["Plain Co"] and "workable" not in plain[0][2]
    assert not resolve.wants_workable(*hits[1], "odd.com") and not resolve.wants_workable(*hits[2], "plain.com")


def test_a_429_that_clears_is_not_a_deferral():
    backoff = resolve.Backoff(base=1, ceiling=4, retries=3, sleep=lambda s: asyncio.sleep(0), clock=lambda: 0.0)
    answers = iter([429, 429, 200])

    async def go():
        deferred = set()
        resolve._DEFERRED.set(deferred)
        async with _client(lambda r: httpx.Response(next(answers), json={}), backoff) as client:
            r = await client.get("https://apply.workable.com/x")
        return r.status_code, deferred
    assert asyncio.run(go()) == (200, set())
    assert "workable" not in backoff.wait                         # reset by the 200


# --- STEP 2: the scoring cap --------------------------------------------------------------

def test_cap_calls_scores_dream_first_and_never_counts_a_pre_filtered_posting():
    import scorer
    jobs = [{"title": "Director of IT", "company": c} for c in ("A", "B", "Dream", "C")]
    jobs.append({"title": "Director of IT", "company": "Acme Insurance"})      # hard-blocked by name: free
    keep, held = scorer.cap_calls(jobs, 2, ["dream"])
    assert [j["company"] for j in keep] == ["Dream", "A", "Acme Insurance"]
    assert [j["company"] for j in held] == ["B", "C"]


def test_a_fetch_run_at_the_cap_holds_the_rest_unseen_and_the_next_run_scores_them(monkeypatch, tmp_path, capsys):
    from tests import test_ops12_api_access_abort as ops12
    import main
    import pending
    import scorer
    import state
    from sources import startups
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pending, "pending_dir", lambda: tmp_path / "state" / "pending")
    (tmp_path / state.STATE_FILE).write_text(json.dumps({"seed": "2026-10-29T00:00:00"}))
    _profile_with(monkeypatch, max_calls_per_run=3)
    postings = [ops12._job(f"https://boards.greenhouse.io/c{i}/jobs/{i}", f"Company {i}") for i in range(5)]
    monkeypatch.setattr(main, "SOURCES", [(n, lambda: []) for n, _ in main.SOURCES])
    monkeypatch.setattr(main, "fetch_startups",
                        lambda slot=None: startups.FetchResult([dict(j) for j in postings], complete=True))
    for name, value in (("load_connections", lambda: {}), ("find_warm_contact", lambda c, k: ""),
                        ("resolve_jobs", lambda jobs: None), ("get_declined_feedback", lambda: [])):
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    import llm
    fake = ops12.FakeProvider(reply=ops12.GOOD_SCORE)
    monkeypatch.setattr(llm, "_provider", fake)

    main.SOURCE_OK.clear()
    main.fetch_run()
    out = capsys.readouterr().out
    assert len(fake.calls) == 3
    assert "[Scorer] cap reached: 2 postings held for the next run" in out
    assert "held by the scoring cap −2" in out
    assert "scorer: cap reached: 2 postings held for the next run" in out      # the fetch summary
    _, queued, _ = pending.load_pending()
    assert sorted(j["apply_link"] for j in queued) == [p["apply_link"] for p in postings[:3]]
    seen = json.loads((tmp_path / state.STATE_FILE).read_text())
    assert len(state.load_url_index(seen)) == 4                  # seed + three scored; two unseen
    assert state.load_deferrals(seen) == {}                      # no deferral count

    main.SOURCE_OK.clear()
    main.fetch_run()                                             # the same five served again
    assert len(fake.calls) == 5
    _, queued, _ = pending.load_pending()
    assert sorted(j["apply_link"] for j in queued) == [p["apply_link"] for p in postings]


def test_the_example_sets_the_cap():
    assert profile_loader.load(ROOT / "profile.example.yaml")["scoring"]["max_calls_per_run"] == 150


# --- STEP 3: the website ladder -----------------------------------------------------------

class FakeHttp:
    """pages: {url prefix: (status, html)}; heads default to the page's status."""

    def __init__(self, pages):
        self.pages, self.gets, self.heads = pages, [], []

    def _find(self, url):
        for prefix, answer in self.pages.items():
            if url.rstrip("/") == prefix.rstrip("/"):
                return answer
        return 0, ""

    def head(self, url):
        self.heads.append(url)
        return self._find(url)[0], url, ""

    def get(self, url):
        self.gets.append(url)
        status, html = self._find(url)
        return status, url, html


def _page(title, body=""):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


class FakeWikidata:
    def __init__(self, sites):
        self.sites, self.asked = sites, []

    def websites(self, name):
        self.asked.append(name)
        return self.sites.get(name, [])


def test_the_list_website_wins_over_wikidata_once_verified():
    http = FakeHttp({"https://acme.com": (200, _page("Acme Robotics | Home"))})
    wiki = FakeWikidata({"Acme Robotics Inc": ["https://acme-other.com"]})
    ladder = websites.Ladder(http, wiki)
    assert ladder.find({"name": "Acme Robotics Inc", "website": "acme.com"}) == ("https://acme.com", "list")
    assert wiki.asked == []


def test_an_unverified_list_website_falls_to_wikidata():
    http = FakeHttp({"https://acme.com": (200, _page("Parked domain")),
                     "https://www.acmerobotics.com": (200, _page("Acme Robotics"))})
    ladder = websites.Ladder(http, FakeWikidata({"Acme Robotics Inc": ["https://www.acmerobotics.com"]}))
    assert ladder.find({"name": "Acme Robotics Inc", "website": "acme.com"}) == ("https://www.acmerobotics.com",
                                                                                 "wikidata")
    assert ("Acme Robotics Inc", "list", "acme.com") in ladder.rejected


def test_a_domain_guess_is_verified_and_a_wrong_one_rejected():
    http = FakeHttp({"https://zephyrgrid.com": (200, _page("Welcome", "Domain for sale")),
                     "https://zephyr-grid.com": (200, _page("Zephyr Grid: storage for the grid"))})
    ladder = websites.Ladder(http, FakeWikidata({}))
    assert ladder.find({"name": "Zephyr Grid Holdings, Inc.", "website": ""}) == ("https://zephyr-grid.com", "guess")
    assert http.heads[:2] == ["https://zephyrgrid.com", "https://zephyr-grid.com"]
    assert "https://zephyrgrid.com" in http.gets                 # HEAD 2xx, so a GET; the page fails the check


def test_a_guess_whose_head_is_not_2xx_is_never_fetched():
    http = FakeHttp({"https://zephyrgrid.com": (404, "")})
    websites.Ladder(http, FakeWikidata({})).find({"name": "Zephyr Grid", "website": ""})
    assert "https://zephyrgrid.com" in http.heads and "https://zephyrgrid.com" not in http.gets


def test_the_model_rung_is_off_by_default_and_logs_a_rejected_guess(monkeypatch):
    http = FakeHttp({"https://orbitwerks.example": (200, _page("Something else"))})
    ladder = websites.Ladder(http, FakeWikidata({}))
    assert ladder.guesser is None
    assert ladder.find({"name": "Orbitwerks", "website": ""}) == ("", "")

    import llm
    from tests.llm_stub import StubProvider
    stub = StubProvider(lambda call: '{"website": "orbitwerks.example"}',
                        usage={"input_tokens": 120, "output_tokens": 20})
    monkeypatch.setattr(llm, "_provider", stub)
    logged = []
    guesser = websites.ModelGuesser("claude-haiku-4-5")
    ladder = websites.Ladder(http, FakeWikidata({}), guesser, log=logged.append)
    assert ladder.find({"name": "Orbitwerks", "website": "", "state": "CO"}) == ("", "")
    assert len(stub.calls) == 1 and guesser.input_tokens == 120
    assert stub.calls[0]["model"] == "claude-haiku-4-5" and stub.calls[0]["json_mode"]
    assert logged == ["[Websites] model guess rejected: Orbitwerks -> https://orbitwerks.example "
                      "(page does not name the company)"]
    assert guesser.cost() == pytest.approx(120 / 1e6 * 1.00 + 20 / 1e6 * 5.00)


def test_the_content_check_needs_the_title_or_a_matching_host():
    journal = _page("ScienceDirect", "Journal of Applied Materials, volume 3")
    assert not websites.page_names_company(journal, "APPLIED MATERIALS INC /DE", "https://www.sciencedirect.com")
    assert websites.page_names_company(_page("Home | JPMorganChase"), "JPMORGAN CHASE & CO",
                                       "https://www.jpmorganchase.com")
    assert websites.page_names_company(_page("AMD - together we advance", "© 2026 Advanced Micro Devices, Inc."),
                                       "ADVANCED MICRO DEVICES INC", "https://www.amd.com")
    # Initials alone are someone else's site (gip.com is not Generation Income Properties).
    assert not websites.page_names_company(_page("GIP | Global Infrastructure Partners"),
                                           "GENERATION INCOME PROPERTIES, INC.", "https://www.gip.com")
    assert websites.page_names_company(_page("Home", "About Lumen Grid today"), "Lumen Grid Corp",
                                       "https://lumengrid.com")


def test_the_homepage_is_checked_and_parked_or_off_host_pages_fail():
    http = FakeHttp({"https://www.sciencedirect.com": (200, _page("ScienceDirect.com")),
                     "https://solidbio.com": (200, _page("SolidBio.com is for sale | HugeDomains", "Solid Biosciences"))})
    assert websites.verify(http, "https://www.sciencedirect.com/journal/applied-materials-today",
                           "APPLIED MATERIALS INC /DE") is None
    assert http.gets == ["https://www.sciencedirect.com"]
    assert websites.verify(http, "solidbio.com", "Solid Biosciences Inc.") is None

    class Redirect(FakeHttp):
        def get(self, url):
            return 200, "https://www.otherco.com/", _page("Zephyr Grid deals at OtherCo")
    assert websites.verify(Redirect({}), "https://zephyrgrid.com", "Zephyr Grid") is None


def test_names_and_guesses():
    assert websites.clean_name("NVIDIA CORP /DE/") == "NVIDIA"
    assert websites.clean_name("LENNAR CORP /NEW/") == "LENNAR"
    assert websites.clean_name("WELLS FARGO & COMPANY/MN") == "WELLS FARGO"
    guesses = websites.domain_guesses("Zephyr Grid Holdings, Inc.")
    assert guesses == ["https://zephyrgrid.com", "https://zephyr-grid.com", "https://zephyrgridcorp.com",
                       "https://zephyrgrid-holdings.com", "https://zephyr.com", "https://zg.com"]
    assert len(websites.domain_guesses("A B C D E F Group")) <= websites.MAX_GUESSES


def test_wikidata_replies_are_cached(tmp_path):
    calls = []

    def get_json(url, params):
        calls.append(params["action"])
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q1"}, {"id": "Q2"}]}
        return {"entities": {"Q1": {"claims": {"P856": [
            {"mainsnak": {"datavalue": {"value": "https://acme.de"}}},
            {"mainsnak": {"datavalue": {"value": "https://www.acme.com/en/about"}}}]}}, "Q2": {}}}
    wd = websites.Wikidata(get_json, str(tmp_path), sleep=lambda s: None)
    assert wd.websites("Acme Inc") == ["https://www.acme.com/en/about"]          # a .com first
    assert websites.Wikidata(get_json, str(tmp_path), sleep=lambda s: None).websites("Acme Inc")
    assert calls == ["wbsearchentities", "wbgetentities"]                       # the second came from the cache


def test_resolve_writes_the_website_source(env):
    paths, _, _ = env

    async def resolver(name, website):
        return {"ats": "greenhouse", "slug": "x", "board_url": "https://boards-api.greenhouse.io/v1/boards/x/jobs"}, {}

    class Ladder:
        guesser = None

        def find(self, row):
            return ("https://bco.com", "guess") if row["name"] == "B Co" else (row.get("website", ""), "list")
    resolve.run(resolver=resolver, paths=paths, yes=True, ladder=Ladder(), max_n=3)
    with open(paths["resolved"]) as fh:
        rows = {r["company"]: r for r in csv.DictReader(fh)}
    assert (rows["B Co"]["website"], rows["B Co"]["website_source"]) == ("https://bco.com", "guess")


# --- STEP 3b: the shipped public boards ------------------------------------------------------

def test_a_boards_file_is_read_first_for_the_names_the_filter_keeps(tmp_path):
    lst = _write(tmp_path / "pub.csv", "name,website,sic,outcome\nKeep Co,,3571,board\nDrop Co,,6022,board\n"
                 "Page Co,,3571,careers_page\n")
    brd = _write(tmp_path / "pub_boards.csv", "name,ats,url,website\n"
                 "Keep Co,greenhouse,https://boards-api.greenhouse.io/v1/boards/keepco/jobs,\n"
                 "Drop Co,lever,https://api.lever.co/v0/postings/dropco?mode=json,\n"
                 "Page Co,careers_page,https://pageco.com/careers,\n")
    got = company_lists.boards([{"file": lst, "tier": "watch", "boards": brd, "filter": {"sic": ["3571"]}}])
    assert [(b["company"], b["slug"]) for b in got] == [("Keep Co", "keepco")]
    errors = []
    company_lists.check_entries([{"file": lst, "tier": "dream", "boards": brd}], "x", errors)
    assert errors == ["x[0].boards: only a watch-tier entry reads a boards file"]


def test_build_public_boards_ships_boards_and_writes_outcomes(tmp_path, monkeypatch):
    from tools import build_public_boards as bpb
    work = tmp_path / "work"
    work.mkdir()
    (work / "startups_ats_resolved.csv").write_text(
        ",".join(resolve.RESOLVED_FIELDS) + "\n"
        "Board Co,,,,,greenhouse,boardco,https://boards-api.greenhouse.io/v1/boards/boardco/jobs,slug,,,https://boardco.com,guess\n"
        "Tenant Co,,,,,workday,tenantco:External,tenantco.wd5.myworkdayjobs.com/External,careers,,,,\n")
    (work / "startups_unresolved.csv").write_text("company,reason,careers_url,website,website_source\n"
                                                  "Page Co,bespoke_page,https://pageco.com/careers,,\n")
    (work / "parked_boards.csv").write_text(
        "ats,board_token,company,board_url,reason,parked_on,last_checked\n"
        "icims,Icims Co,Icims Co,,unsupported vendor: icims,2026-10-29,2026-10-29\n"
        "none,Ghost Co,Ghost Co,,resolve: no_site,2026-10-29,2026-10-29\n")
    lst = tmp_path / "pub.csv"
    lst.write_text("# built\nname,website,website_source,cik,outcome,notes\nBoard Co,,,1,,\nTenant Co,,,2,,\n"
                   "Page Co,,,3,,\nIcims Co,,,4,,\nGhost Co,,,5,,\nLater Co,,,6,,\n")
    out = tmp_path / "boards.csv"
    boards, outcome, sites = bpb.collect(str(work))
    bpb.write_boards(boards, out)
    rows = {r["name"]: r for r in bpb.update_list(outcome, sites, lst)}
    assert {r["name"]: r["outcome"] for r in rows.values()} == {
        "Board Co": "board", "Tenant Co": "workday", "Page Co": "careers_page", "Icims Co": "unsupported:icims",
        "Ghost Co": "none", "Later Co": ""}
    assert (rows["Board Co"]["website"], rows["Board Co"]["website_source"]) == ("https://boardco.com", "guess")
    assert lst.read_text().startswith("# built\n")
    with open(out) as fh:
        shipped = list(csv.DictReader(fh))
    assert list(shipped[0]) == ["name", "ats", "url", "website"]
    assert [(r["name"], r["ats"]) for r in shipped] == [("Board Co", "greenhouse"), ("Tenant Co", "workday"),
                                                        ("Page Co", "careers_page")]


def test_build_public_boards_marks_workable_unchecked_and_on_request_untried(tmp_path):
    from tools import build_public_boards as bpb
    work = tmp_path / "work"
    work.mkdir()
    (work / "parked_boards.csv").write_text(
        "ats,board_token,company,board_url,reason,parked_on,last_checked\n"
        "none,Wait Co,Wait Co,,resolve: workable not probed (blocked),2026-10-30,2026-10-30\n"
        "none,Late Co,Late Co,,resolve: workable not probed (batch ended),2026-10-30,2026-10-30\n"
        "none,Gone Co,Gone Co,,resolve: no_site,2026-10-30,2026-10-30\n")
    (work / "resolve_pending.csv").write_text(",".join(resolve.PENDING_FIELDS) + "\n"
                                              "Busy Co,deferred,personio,,,,,2026-10-29\n")
    lst = tmp_path / "pub.csv"
    lst.write_text("name,website,website_source,cik,outcome,notes\nWait Co,,,1,,\nBusy Co,,,2,,\nLater Co,,,3,,\n"
                   "Late Co,,,4,,\nGone Co,,,5,,\n")
    _, outcome, sites = bpb.collect(str(work))
    rows = {r["name"]: r["outcome"] for r in bpb.update_list(outcome, sites, lst, mark_untried=True)}
    assert rows == {"Wait Co": "none/workable_unchecked", "Late Co": "none/workable_unchecked",
                    "Gone Co": "none", "Busy Co": "untried", "Later Co": "untried"}
    companies = company_lists.load([{"file": str(lst), "tier": "watch"}])
    assert sorted(c.name for c in companies if resolve.needs_board(c)) == ["Busy Co", "Later Co"]


def test_shipped_starter_boards_header_and_tokens():
    with open(LISTS / "starter_boards.csv", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    assert reader.fieldnames == ["name", "ats", "url", "website"]
    for r in rows:
        if r["ats"] != "careers_page":
            assert company_lists.board_token(r["ats"], r["url"]), r
    public = [r for r in company_lists.read_list(str(LISTS / "starter_companies.csv")) if r["segment"] == "public"]
    assert "outcome" in public[0]
    assert {r["outcome"].split(":")[0] for r in public} <= {"", "board", "workday", "careers_page", "unsupported",
                                                            "none", "none/workable_unchecked", "untried"}


def test_corrected_companies_stay_as_corrected_in_the_shipped_files():
    # corrections.csv is the record: a guessed domain gave these another company's board.
    corrections = {k: v for k, v in company_lists.load_corrections().items() if "outcome" in v}
    assert {"oshkosh corp", "sun communities inc", "velocity financial, inc."} <= set(corrections)
    with open(LISTS / "starter_boards.csv", newline="") as fh:
        assert not set(corrections) & {r["name"].casefold() for r in csv.DictReader(fh)}
    with open(LISTS / "starter_companies.csv", newline="") as fh:
        rows = csv.DictReader(line for line in fh if not line.startswith("#"))
        got = {r["name"].casefold(): r["outcome"] for r in rows if r["name"].casefold() in corrections}
    assert got == {k: v["outcome"] for k, v in corrections.items()}


# --- STEP 4: cost reporting -----------------------------------------------------------------

def test_cost_arithmetic_from_the_usage_fields():
    import calibrate
    cost = calibrate.cost_summary({"calls": 4, "input": 24_000, "output": 2_000, "model": "claude-sonnet-4-6"},
                                  per_day=30)
    per_call = (6_000 * 3.00 + 500 * 15.00) / 1e6
    assert (cost["avg_input"], cost["avg_output"]) == (6_000, 500)
    assert cost["run"] == pytest.approx(4 * per_call) and cost["per_100"] == pytest.approx(100 * per_call)
    assert cost["per_month"] == pytest.approx(per_call * 30 * 30)
    lines = calibrate.cost_lines(cost)
    assert lines[0].startswith("Cost: 4 scoring calls at claude-sonnet-4-6, on average 6,000 input and 500 output")
    assert "100 scored postings ≈ $2.55; a typical month at 30 a day ≈ $22.95" in lines[1]


def test_an_unknown_model_reports_unknown():
    import calibrate
    assert config.model_cost("claude-made-up", 1, 1) is None and config.format_cost(None) == "unknown"
    lines = calibrate.cost_lines(calibrate.cost_summary({"calls": 1, "input": 5, "output": 5, "model": "x-1"}))
    assert "this run unknown" in lines[0] and "not in config.MODEL_PRICES" in lines[1]


def test_calibrate_counts_tokens_from_the_api_usage(monkeypatch):
    import calibrate
    import scorer
    from tests import test_ops12_api_access_abort as ops12

    import llm
    from tests.llm_stub import StubProvider
    monkeypatch.setattr(llm, "_provider", StubProvider(lambda call: json.dumps(ops12.GOOD_SCORE),
                                                       usage={"input_tokens": 5_000, "output_tokens": 300}))
    monkeypatch.setattr(scorer.time, "sleep", lambda s: None)
    jobs = [{"title": "Director of IT", "company": f"C{i}", "location": "Remote, US", "work_model": "remote",
             "description": "d", "file": f"f{i}"} for i in range(2)]
    calibrate.score(jobs)
    assert calibrate.LAST_USAGE == {"calls": 2, "input": 10_000, "output": 600, "model": scorer.MODEL}


def test_doctor_prints_model_cap_and_per_call_cost():
    import doctor
    [line] = doctor.scoring_cost()
    per_call = config.model_cost("claude-sonnet-4-6", *config.SCORING_TOKENS_PER_CALL)
    assert line["status"] == "PASS"
    assert line["reason"].startswith("model claude-sonnet-4-6, at most 100 scoring calls a fetch run; about "
                                     f"{config.format_cost(per_call)} a call")


# --- STEP 5: rationale example ---------------------------------------------------------------

def test_rationale_example_renders_in_the_prompt():
    import scorer
    active = profile_loader.active()
    example = profile_loader.load(ROOT / "profile.example.yaml")
    assert f'Example: "{active["roles"]["rationale_example"]}"' in scorer.build_system_prompt(active)
    text = scorer.build_system_prompt(example)
    assert f'Example: "{example["roles"]["rationale_example"]}"' in text
    assert "top IT role" not in example["roles"]["rationale_example"]
