"""PUB-11: company lists as the public input — the list format and filters,
tiers, the shipped lists, `main.py resolve`, the public-company build tool,
the department wording fields."""

import builtins
import csv
import json
import os
from pathlib import Path

import pytest

import company_lists
import profile_loader
import resolve
from sources import startups

ROOT = Path(__file__).resolve().parent.parent
LISTS = ROOT / "data" / "company_lists"


def _write(path, text):
    path.write_text(text)
    return str(path)


# --- STEP 1: the list format ---------------------------------------------------------------

def test_read_list_keeps_empty_websites_and_skips_comments(tmp_path):
    path = _write(tmp_path / "a.csv", "# built today\nname,website,notes\nAcme,,\nBeta, https://beta.io ,hq DC\n,,\n")
    rows = company_lists.read_list(path)
    assert [r["name"] for r in rows] == ["Acme", "Beta"]
    assert rows[0]["website"] == "" and rows[1]["website"] == "https://beta.io" and rows[1]["notes"] == "hq DC"


def test_a_list_without_a_name_column_is_refused(tmp_path):
    with pytest.raises(ValueError, match="name"):
        company_lists.read_list(_write(tmp_path / "a.csv", "company,website\nAcme,\n"))


def test_first_file_in_profile_order_wins_the_tier(tmp_path):
    a = _write(tmp_path / "a.csv", "name,website,notes\nAcme,,\nBeta,,\n")
    b = _write(tmp_path / "b.csv", "name,website,notes\nACME,acme.com,\nGamma,,\n")
    got = company_lists.load([{"file": a, "tier": "dream"}, {"file": b, "tier": "watch"}])
    assert [(c.name, c.tier) for c in got] == [("Acme", "dream"), ("Beta", "dream"), ("Gamma", "watch")]
    assert company_lists.dream_names([{"file": a, "tier": "dream"}, {"file": b, "tier": "watch"}]) == ["Acme", "Beta"]


def test_dream_tier_lists_join_the_dream_companies(tmp_path):
    dream = _write(tmp_path / "mine.csv", "name,website,notes\nAcme,,\nExample Labs,,\n")
    profile = profile_loader.with_defaults(profile_loader.active())
    profile["sources"]["company_lists"] = [{"file": dream, "tier": "dream"}]
    names = profile_loader.dream_companies(profile)
    base = profile_loader.dream_companies()
    assert names[:len(base)] == base and names[len(base):] == ["Acme"]       # Example Labs already listed


# --- Filters ---------------------------------------------------------------------------------

ROWS = [{"name": "Acme", "sic": "7372", "state": "MD"}, {"name": "Beta", "sic": "3812", "state": "VA"},
        {"name": "Gamma", "sic": "2834", "state": "CA"}, {"name": "Delta", "sic": "", "state": ""}]


@pytest.mark.parametrize("flt,expected", [
    (None, ["Acme", "Beta", "Gamma", "Delta"]),
    ({"sic": ["7370-7379"]}, ["Acme"]),
    ({"sic": ["7370-7379", "3812"]}, ["Acme", "Beta"]),
    ({"states": ["md", "VA"]}, ["Acme", "Beta"]),
    ({"names": ["gamma", "Delta"]}, ["Gamma", "Delta"]),
    ({"exclude_names": ["ACME"]}, ["Beta", "Gamma", "Delta"]),
    ({"sic": ["3800-3899"], "states": ["CA"]}, []),
])
def test_filters(flt, expected):
    assert [r["name"] for r in ROWS if company_lists.matches(r, flt)] == expected


def test_profile_validation_of_list_entries(tmp_path):
    raw = profile_loader.read()
    ok = str(LISTS / "my_companies.csv")
    raw["sources"]["company_lists"] = [
        {"file": ok, "tier": "dream"},
        {"file": "data/company_lists/missing.csv", "tier": "watch"},
        {"file": ok, "tier": "favourite"},
        {"file": ok, "tier": "watch", "filter": {"sic": ["73", "7379-7370"], "states": ["ZZ"], "colour": ["x"]}},
    ]
    errors = [e for e in profile_loader.validate(raw) if "company_lists" in e]
    assert any("[1].file: data/company_lists/missing.csv does not exist" in e for e in errors)
    assert any("[2].tier: must be dream or watch" in e for e in errors)
    assert any("filter.sic" in e and "'73'" in e and "'7379-7370'" in e for e in errors)
    assert any("filter.states" in e and "ZZ" in e for e in errors)
    assert any("filter.colour: unknown filter" in e for e in errors)
    assert len(errors) == 5


# --- STEP 2: the shipped lists -----------------------------------------------------------------

def test_starter_boards_header_is_exact_and_carries_no_harvest_column():
    with open(LISTS / "starter_boards.csv", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == ["name", "ats", "url", "website"]


def test_every_starter_board_url_gives_back_its_token():
    rows = company_lists.read_list(LISTS / "starter_boards.csv")
    assert rows and all(company_lists.board_token(r["ats"], r["url"]) for r in rows if r["ats"] != "careers_page")
    startup = {r["name"].casefold() for r in company_lists.read_list(LISTS / "starter_companies.csv")
               if r["segment"] == "startup"}
    assert all(r["website"] == "" for r in rows if r["name"].casefold() in startup)


@pytest.mark.skipif(not startups.RESOLVED_CSV.exists(), reason="the private resolved CSV is not in this checkout")
def test_starter_boards_matches_its_generator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_starter_boards", ROOT / "tools" / "build_starter_boards.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    built = mod.rows()
    # PUB-15: the startup rows come first; the public segment's boards follow.
    assert company_lists.read_list(LISTS / "starter_boards.csv")[:len(built)] == built


def test_my_companies_ships_with_the_header_only():
    assert (LISTS / "my_companies.csv").read_text() == "name,website,notes\n"


def test_starter_companies_columns_and_provenance_line():
    path = LISTS / "starter_companies.csv"
    first, header = path.read_text().splitlines()[:2]
    assert first.startswith("# Built ") and "SEC EDGAR" in first and "Wikidata (CC0)" in first
    assert header == "name,website,notes,segment,ticker,sic,sic_description,state,outcome"
    rows = company_lists.read_list(path)
    public = [r for r in rows if r["segment"] == "public"]
    assert len(public) > 1000 and all(r["ticker"] for r in public)
    assert {r["segment"] for r in rows} == {"public", "startup"}


# --- STEP 3: the committed profile fetches the same boards -------------------------------------

def _key(boards):
    return [(b["company"], b["ats"], b["slug"], b["board_url"]) for b in boards]


@pytest.mark.skipif(not startups.RESOLVED_CSV.exists(), reason="the private resolved CSV is not in this checkout")
def test_committed_profile_fetches_the_same_board_set(monkeypatch, capsys):
    with monkeypatch.context() as m:
        m.setattr(startups, "_list_boards", lambda: [])
        before = _key(startups.load_boards(include_parked=True))      # the resolved CSV alone (pre-PUB-11)
    both = _key(startups.load_boards(include_parked=True))
    monkeypatch.setattr(startups, "RESOLVED_CSV", Path("/nonexistent/startups_ats_resolved.csv"))
    lists_only = _key(startups.load_boards(include_parked=True))      # a public clone: the list alone
    assert len(before) > 5000
    assert before == both == lists_only


def test_committed_profile_lists_the_startup_segment_as_watch():
    assert profile_loader.active()["sources"]["company_lists"] == (
        {"file": "data/company_lists/starter_companies.csv", "tier": "watch",
         "boards": "data/company_lists/starter_boards.csv", "filter": {"segment": ("startup",)}},)


def test_example_profile_lists():
    example = profile_loader.load(str(ROOT / "profile.example.yaml"))
    entries = example["sources"]["company_lists"]
    assert [(e["file"].split("/")[-1], e["tier"]) for e in entries] == [
        ("starter_companies.csv", "watch"), ("my_companies.csv", "dream")]
    assert entries[0]["boards"].endswith("starter_boards.csv")
    assert tuple(entries[0]["filter"]["segment"]) == ("public", "startup") and entries[0]["filter"]["sic"]


# --- STEP 4: resolve ---------------------------------------------------------------------------

HITS = {
    "Board Co": ({"name": "Board Co", "ats": "greenhouse", "slug": "boardco", "resolved_at": "2026-10-28",
                  "board_url": "https://boards-api.greenhouse.io/v1/boards/boardco/jobs?content=true"}, {}),
    "Tenant Co": ({"name": "Tenant Co", "ats": "workday", "slug": "tenantco:External",
                   "board_url": "tenantco.wd1.myworkdayjobs.com/en-US/External", "source": "careers_page"}, {}),
    "Page Co": (None, {"reason": "bespoke_page", "careers_url": "https://pageco.com/careers"}),
    "Icims Co": (None, {"reason": "bespoke_page", "careers_url": "https://careers-icimsco.icims.com/jobs"}),
    "Taleo Co": (None, {"reason": "unsupported_ats:taleo", "careers_url": "https://taleoco.com/careers"}),
    "Ghost Co": (None, {"reason": "no_site", "careers_url": ""}),
    "Dream Board": ({"name": "Dream Board", "ats": "lever", "slug": "dreamboard", "resolved_at": "2026-10-28",
                     "board_url": "https://api.lever.co/v0/postings/dreamboard?mode=json"}, {}),
    "Dream Tenant": ({"name": "Dream Tenant", "ats": "workday", "slug": "dt:Careers",
                      "board_url": "dt.wd5.myworkdayjobs.com/Careers"}, {}),
    "Dream Page": (None, {"reason": "bespoke_page", "careers_url": "https://dreampage.com/jobs"}),
}


@pytest.fixture
def lists_env(tmp_path, monkeypatch):
    watch = _write(tmp_path / "watch.csv", "name,website,notes\nBoard Co,,\nTenant Co,,\nPage Co,pageco.com,\n"
                   "Icims Co,,\nTaleo Co,,\nGhost Co,,\nOld Co,,\nCarried Co,,\n")
    # A list row that carries its board is never resolved.
    carried = _write(tmp_path / "carried.csv", "name,ats,url,website\n"
                     "Carried Co,ashby,https://api.ashbyhq.com/posting-api/job-board/carried,\n")
    dream = _write(tmp_path / "dream.csv", "name,website,notes\nDream Board,,\nDream Tenant,,\nDream Page,,\n")
    profile = profile_loader.with_defaults(profile_loader.active())
    profile["sources"]["company_lists"] = [{"file": carried, "tier": "watch"}, {"file": watch, "tier": "watch"},
                                           {"file": dream, "tier": "dream"}]
    monkeypatch.setattr(profile_loader, "active", lambda: profile_loader._freeze(profile))
    paths = {"resolved": str(tmp_path / "resolved.csv"), "parked": str(tmp_path / "parked.csv"),
             "bespoke": str(tmp_path / "unresolved.csv"), "companies": str(tmp_path / "companies.yaml")}
    Path(paths["parked"]).write_text("ats,board_token,company,board_url,reason,parked_on,last_checked\n"
                                     "none,Old Co,Old Co,,resolve: no_site,2026-10-01,2026-10-01\n")
    Path(paths["companies"]).write_text("companies: []\nunresolved: []\npinned:\n  careers_pages: {}\n")
    writes = []
    real = builtins.open

    def recording(file, mode="r", *a, **k):
        if set(str(mode)) & set("wax+"):
            writes.append(os.fspath(file))
        return real(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", recording)
    asked = []

    async def resolver(name, website):
        asked.append((name, website))
        return HITS[name]

    return paths, resolver, asked, writes


def test_resolve_writes_each_outcome_where_the_engine_reads_it(lists_env, capsys):
    import yaml

    import calibrate
    paths, resolver, asked, writes = lists_env
    assert resolve.run(resolver=resolver, paths=paths, today="2026-10-28", yes=True) == 0
    assert {n for n, _ in asked} == set(HITS)                    # Old Co was tried; Carried Co has a board
    assert ("Page Co", "pageco.com") in asked
    with open(paths["resolved"]) as fh:
        resolved = list(csv.DictReader(fh))
    assert [(r["company"], r["ats"], r["board_token"]) for r in resolved] == [
        ("Board Co", "greenhouse", "boardco"), ("Tenant Co", "workday", "tenantco:External")]
    with open(paths["bespoke"]) as fh:
        assert [(r["company"], r["reason"], r["careers_url"]) for r in csv.DictReader(fh)] == [
            ("Page Co", "bespoke_page", "https://pageco.com/careers")]
    with open(paths["parked"]) as fh:
        parked = {r["company"]: r for r in csv.DictReader(fh)}
    assert parked["Icims Co"]["reason"] == "unsupported vendor: icims"
    assert parked["Taleo Co"]["reason"] == "unsupported vendor: taleo"
    assert parked["Ghost Co"]["reason"] == "resolve: no_site" and parked["Ghost Co"]["parked_on"] == "2026-10-28"
    assert "Old Co" in parked
    data = yaml.safe_load(Path(paths["companies"]).read_text())
    assert [c["name"] for c in data["companies"]] == ["Dream Board"]
    assert data["pinned"]["workday"]["Dream Tenant"] == {"host": "dt.wd5.myworkdayjobs.com", "tenant": "dt",
                                                         "site": "Careers"}
    assert data["pinned"]["careers_pages"]["Dream Page"] == "https://dreampage.com/jobs"
    out = capsys.readouterr().out
    assert "  board                    2" in out and "  unsupported vendor       2" in out
    assert "unsupported vendors: icims 1, taleo 1" in out or "unsupported vendors: taleo 1, icims 1" in out
    assert "within capacity" in out or "cadence" in out
    assert [w for w in writes if calibrate._protected(w)] == []
    assert set(writes) <= set(paths.values())


def test_resolve_report_only_writes_nothing(lists_env, capsys):
    paths, resolver, asked, writes = lists_env
    before = {p: Path(p).read_text() if Path(p).exists() else None for p in paths.values()}
    assert resolve.run(report_only=True, max_n=3, resolver=resolver, paths=paths, yes=True) == 0
    assert len(asked) == 3 and writes == []
    assert {p: Path(p).read_text() if Path(p).exists() else None for p in paths.values()} == before
    assert "--report-only: nothing written" in capsys.readouterr().out


def test_resolve_list_must_be_in_the_profile(lists_env, capsys):
    paths, resolver, _, _ = lists_env
    assert resolve.run(list_file="data/company_lists/nope.csv", resolver=resolver, paths=paths) == 1


@pytest.mark.parametrize("url,vendor", [
    ("https://careers-acme.icims.com/jobs", "icims"), ("https://career4.successfactors.com/career?company=x", "successfactors"),
    ("https://jobs.sapsf.eu/acme", "successfactors"), ("https://acme.phenompeople.com/us/en", "phenom"),
    ("https://acme.clearcompany.com/careers", "clearcompany"), ("https://acme.pinpointhq.com/", "pinpoint"),
    ("https://acme.com/careers", None),
])
def test_unsupported_vendor_by_url(url, vendor):
    assert resolve.unsupported_vendor(url) == vendor


def test_resolve_ats_workflow_calls_resolve():
    assert "python main.py resolve" in (ROOT / ".github" / "workflows" / "resolve_ats.yml").read_text()


# --- The public-company build tool ---------------------------------------------------------

def _load_build():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_public_companies", ROOT / "tools" / "build_public_companies.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_tool_with_stubbed_http(tmp_path):
    mod = _load_build()
    tickers = {"fields": ["cik", "name", "ticker", "exchange"],
               "data": [[1, "ACME CORP", "ACME", "Nasdaq"], [1, "ACME CORP", "ACMEW", "Nasdaq"],
                        [2, "Beta  Inc", "BETA", "NYSE"], [3, "Gamma plc", "GAM", "NYSE"],
                        [4, "Otc Shell", "OTCS", "OTC"]]}
    subs = {1: {"sic": 7372, "sicDescription": "Prepackaged Software", "addresses": {"business": {"stateOrCountry": "MD"}}},
            2: {"sic": 3812, "sicDescription": "Search & Navigation", "website": "beta.com",
                "addresses": {"business": {"stateOrCountry": "VA"}}},
            3: {"sic": 2834, "sicDescription": "Pharma", "addresses": {"business": {"stateOrCountry": "X0"}}}}
    calls = []

    def get(url):
        calls.append(url)
        if url == mod.TICKERS_URL:
            return tickers
        return subs[int(url.split("CIK")[1].split(".")[0])]

    sparql = {"results": {"bindings": [{"cik": {"value": "0000000001"}, "website": {"value": "https://www.acme.com/"}},
                                       {"cik": {"value": "1"}, "website": {"value": "https://www.acme.com/about"}}]}}
    sleeps = []
    sec = mod.Sec(get, cache_dir=tmp_path / "cache", sleep=sleeps.append, clock=lambda: 0.0)
    rows = mod.build(sec, lambda q: sparql, log=lambda *a: None)
    # build() leaves the website to the ladder (PUB-12): it carries the Wikidata and SEC
    # candidates in two helper columns that write() drops.
    assert [list(r) for r in rows] == [mod.COLUMNS + ["_wikidata", "_sec"]] * 2
    assert [(r["name"], r["_wikidata"] or r["_sec"], r["cik"], r["ticker"], r["sic"], r["state"])
            for r in rows] == [
        ("ACME CORP", "https://www.acme.com", "1", "ACME", "7372", "MD"),
        ("Beta Inc", "https://beta.com", "2", "BETA", "3812", "VA")]          # Gamma is not US-based; OTC dropped
    assert all(s <= mod.SEC_INTERVAL for s in sleeps) and len(sleeps) == len(calls)
    mod.write(rows, tmp_path / "out.csv", today="2026-10-28")
    text = (tmp_path / "out.csv").read_text().splitlines()
    assert text[0].startswith("# Built 2026-10-28") and text[1] == ",".join(mod.COLUMNS)
    # A re-run reads the cache: only the tickers file is fetched again.
    calls.clear()
    mod.build(mod.Sec(get, cache_dir=tmp_path / "cache", sleep=lambda s: None), lambda q: sparql, log=lambda *a: None)
    assert calls == [mod.TICKERS_URL]


def test_build_tool_requires_a_contact_for_sec(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    with pytest.raises(SystemExit, match="GMAIL_ADDRESS"):
        _load_build().main(["--out", "/nonexistent/public.csv"])


def test_build_tool_needs_an_out_path_so_it_never_overwrites_the_starter_list(capsys):
    """PUB-07b: the build is the public segment only; build_starter_companies.py merges it."""
    with pytest.raises(SystemExit):
        _load_build().main([])
    assert "--out" in capsys.readouterr().err


# --- STEP 6: department wording ------------------------------------------------------------

import prompt_template  # noqa: E402


def _prompt(raw):
    return prompt_template.render((ROOT / "prompts" / "scoring_system.txt").read_text(),
                                  prompt_template.context(raw))


def test_department_examples_and_top_seat_render():
    text = _prompt(profile_loader.read(str(ROOT / "profile.example.yaml")))
    # PUB-16: the example persona is a Director of FP&A.
    assert ('- "in_department": in that department or function, but financial analyst, FP&A manager, '
            'or finance business partner,\n  with no team or budget.') in text
    assert '- "outside_department": not in that department or function: accounting and controllership, ' \
           'treasury, tax, audit, finance systems, investor relations, sales operations.' in text
    assert "first whether it is the top FP&A seat (no VP of FP&A above it) or reports into one," in text
    assert '"role": "<top FP&A seat (no VP of FP&A above it) or reports into one; what it owns, as keywords>"' in text


def test_monthly_probe_leaves_resolve_parked_rows_alone(monkeypatch):
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("probe_parked_boards", ROOT / "tools" / "probe_parked_boards.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parked = {("none", "Ghost Co"): {"ats": "none", "board_token": "Ghost Co", "company": "Ghost Co",
                                    "board_url": "", "reason": "resolve: no_site"},
              ("icims", "Icims Co"): {"ats": "icims", "board_token": "Icims Co", "company": "Icims Co",
                                     "board_url": "https://x.icims.com", "reason": "unsupported vendor: icims"},
              ("ashby", "gone"): {"ats": "ashby", "board_token": "gone", "company": "Gone",
                                 "board_url": "https://api.ashbyhq.com/posting-api/job-board/gone", "reason": "404"}}
    probed, saved = [], []
    monkeypatch.setattr(mod.startups, "read_parked", lambda: dict(parked))
    monkeypatch.setattr(mod.startups, "load_boards", lambda include_parked=False: [])
    monkeypatch.setattr(mod.startups, "write_parked", lambda rows: saved.extend(rows))
    monkeypatch.setattr(mod, "_watchlist_tenants", lambda: [])
    monkeypatch.setattr(mod, "_probe", lambda b: (probed.append(b["company"]), (b, "404"))[1])
    monkeypatch.setattr(sys, "argv", ["probe"])
    mod.main()
    assert probed == ["Gone"]
    assert {r["company"] for r in saved} == {"Ghost Co", "Icims Co", "Gone"}
