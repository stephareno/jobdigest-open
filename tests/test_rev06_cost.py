"""REV-06: a daily ceiling on fetch's model calls, a per-run cap on Bespoke
extractions, and prompt caching with cache-aware costs.

Covers: the ceiling reached mid-run holds the rest unseen (dream companies
scored first) and resets on the next local day; the count survives across
the day's three runs and includes Bespoke extractions; the digest banner
names the ceiling day once; the Bespoke cap stops extraction with the cursor
on the first page left; fetch_all takes the lower of its cap and the day's
remainder; cache tokens flow into Usage, the scorer's counters, calibrate and
the cost arithmetic; the system block carries cache_control for Anthropic and
never reaches the stub provider. No network access, no model calls.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops04_sharded_fetch import _job, main, pending, pipeline, state, workdir  # noqa: E402,F401
from test_bespoke import _Clock, _checker, _companies, _extractor  # noqa: E402

import calibrate  # noqa: E402
import config  # noqa: E402
import fetch_budget  # noqa: E402
import llm  # noqa: E402
import scorer  # noqa: E402
import sources.bespoke as bespoke  # noqa: E402
from tests.llm_stub import StubProvider  # noqa: E402


@pytest.fixture
def day(monkeypatch):
    """The local day fetch_budget keys on, settable by the test."""
    current = {"day": "2026-11-09"}
    monkeypatch.setattr(fetch_budget, "today", lambda: current["day"])
    return current


def _ceiling(monkeypatch, n):
    monkeypatch.setattr(fetch_budget, "ceiling", lambda: n)


def _postings(n, start=0, dream=()):
    return [{**_job(f"https://boards.greenhouse.io/co{i}/jobs/{i}", company=f"Co{i}",
                    title=f"Director of IT {i}"), "is_dream_company": i in dream}
            for i in range(start, start + n)]


def _queued_urls():
    return sorted(j["apply_link"] for j in pending.load_pending()[1])


def _unseen(urls):
    index = state.load_url_index()
    return [u for u in urls if state.job_hash(u) not in index]


# --------------------------------------------------------------- the ceiling

def test_ceiling_reached_mid_run_holds_the_rest_unseen_and_resets_next_day(pipeline, monkeypatch, day, capsys):
    calls, catalog = pipeline
    _ceiling(monkeypatch, 3)
    catalog["Startup"] = _postings(5, dream=(4,))
    main.fetch_run(shard=None)

    queued = _queued_urls()
    assert len(queued) == 3
    assert "https://boards.greenhouse.io/co4/jobs/4" in queued          # the dream company first
    held = [j["apply_link"] for j in catalog["Startup"] if j["apply_link"] not in queued]
    assert len(held) == 2 and _unseen(held) == held                      # held unseen, not dropped
    out = capsys.readouterr().out
    assert ("[Scorer] daily scoring ceiling reached: 2 postings held unseen for a later run "
            "(scoring.max_calls_per_day 3, 3 calls were left today 2026-11-09)") in out
    assert "scorer: daily scoring ceiling reached: 2 postings held" in out   # fetch summary line
    entry = fetch_budget.load()["days"]["2026-11-09"]
    assert entry == {"scoring_calls": 3, "extractions": 0, "ceiling": 3, "held": 2, "reached": True}

    main.fetch_run(shard=None)                                           # same day: nothing left
    assert len(_queued_urls()) == 3

    day["day"] = "2026-11-10"                                            # local midnight passed
    main.fetch_run(shard=None)
    assert len(_queued_urls()) == 5 and _unseen(held) == []
    assert fetch_budget.load()["days"]["2026-11-10"]["scoring_calls"] == 2


def test_the_count_survives_across_the_three_runs_of_a_day(pipeline, monkeypatch, day):
    calls, catalog = pipeline
    _ceiling(monkeypatch, 5)
    for run in range(3):
        catalog["Startup"] = _postings(2, start=run * 2)
        main.fetch_run(shard=None)
    entry = fetch_budget.load()["days"]["2026-11-09"]
    assert entry["scoring_calls"] == 5 and entry["held"] == 1 and entry["reached"]
    assert len(_queued_urls()) == 5


def test_bespoke_extractions_count_toward_the_ceiling(pipeline, monkeypatch, day):
    calls, catalog = pipeline
    _ceiling(monkeypatch, 4)
    fetch_budget.RUN["extractions"] = 3                                  # as bespoke.fetch_all adds them
    monkeypatch.setattr(fetch_budget, "reset_run", lambda: None)
    catalog["Startup"] = _postings(3)
    main.fetch_run(shard=None)
    assert len(_queued_urls()) == 1
    entry = fetch_budget.load()["days"]["2026-11-09"]
    assert entry["extractions"] == 3 and entry["scoring_calls"] == 1 and entry["held"] == 2


def test_under_the_ceiling_the_per_run_cap_still_names_itself(pipeline, monkeypatch, day, capsys):
    calls, catalog = pipeline
    _ceiling(monkeypatch, 600)
    catalog["Startup"] = _postings(1)
    main.fetch_run(shard=None)
    assert "daily scoring ceiling" not in capsys.readouterr().out
    assert fetch_budget.load()["days"]["2026-11-09"]["reached"] is False


def test_record_keeps_two_weeks(day, monkeypatch):
    _ceiling(monkeypatch, 10)
    for d in range(1, 21):
        day["day"] = f"2026-11-{d:02d}"
        fetch_budget.record(1, 0, 0)
    assert sorted(fetch_budget.load()["days"]) == [f"2026-11-{d:02d}" for d in range(7, 21)]


def test_the_digest_banner_names_the_ceiling_day_once(pipeline, monkeypatch, day):
    calls, catalog = pipeline
    _ceiling(monkeypatch, 1)
    catalog["Startup"] = _postings(3)
    main.fetch_run(shard=None)
    notices = []
    monkeypatch.setattr(main, "send_digest", lambda sections, **kw: (calls["emails"].append(sections),
                                                                     notices.append(kw.get("notices"))))
    main.digest_run()
    assert notices[-1] == ["2 postings are waiting; the daily scoring ceiling (1) was reached on 2026-11-09"]

    day["day"] = "2026-11-10"
    catalog["Startup"] = _postings(1, start=10)
    _ceiling(monkeypatch, 600)
    main.fetch_run(shard=None)
    main.digest_run()
    assert len(notices) == 2 and notices[-1] == []                       # reported once


def test_banner_line_wording():
    assert fetch_budget.banner_line("2026-11-09", 1, 600) == \
        "1 posting is waiting; the daily scoring ceiling (600) was reached on 2026-11-09"


def test_profiles_carry_the_ceiling():
    import profile_loader
    root = Path(__file__).resolve().parent.parent
    if (root / "profile.yaml").exists():                 # the public export ships no profile.yaml
        assert profile_loader.load(root / "profile.yaml")["scoring"]["max_calls_per_day"] == 600
    assert profile_loader.load(root / "profile.example.yaml")["scoring"]["max_calls_per_day"] == 300
    assert profile_loader.DEFAULTS["scoring.max_calls_per_day"] == 300


# ------------------------------------------------------------- bespoke cap

def test_the_bespoke_cap_stops_extraction_and_keeps_the_cursor(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=0.0))
    monkeypatch.setattr(bespoke, "listing_is_theirs", lambda *a, **k: "")
    companies = _companies(10)
    run = bespoke.run_rolling(companies, {}, {}, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker(), max_extractions=3)
    assert run.stop_reason == "extraction_cap"
    assert run.stats["extraction_calls"] == 3 and run.stats["extracted"] == 3
    ordered = sorted(companies, key=lambda c: bespoke.order_key(c["company"]))
    assert run.cursor["position"] == bespoke.order_key(ordered[2]["company"])   # the 4th is not done
    assert run.cursor["done_this_pass"] == 3 and len(run.entries) == 3

    # The next pass resumes at the first page left.
    nxt = bespoke.run_rolling(companies, {}, run.cursor, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker(), max_extractions=3)
    assert sorted(nxt.entries) == sorted(c["company"].lower() for c in ordered[3:6])


def test_a_zero_cap_extracts_nothing_and_moves_nothing(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(bespoke, "extract_jobs", _extractor(clock, per_extract=0.0))
    run = bespoke.run_rolling(_companies(4), {}, {"position": ""}, budget_secs=1000, api_ready=True,
                              clock=clock, check=_checker(), max_extractions=0)
    assert run.stats["extraction_calls"] == 0 and run.stop_reason == "extraction_cap"
    assert run.cursor["position"] == "" and run.entries == {}


@pytest.mark.parametrize("profile_cap, daily_left, expected", [(None, 500, 60), (5, 500, 5), (60, 7, 7)])
def test_fetch_all_takes_the_lower_of_its_cap_and_the_day(monkeypatch, capsys, profile_cap, daily_left,
                                                           expected):
    seen = {}

    def fake_rolling(companies, st, cursor, budget, cancel=None, max_extractions=None, **kw):
        seen["cap"] = max_extractions
        out = bespoke.RollingRun()
        out.stats.update(extraction_calls=min(2, max_extractions), companies=0, listings_raw=0,
                         content_check_ok=0, passed_title_filter=0, rejected_other_employer=0)
        out.stop_reason = "extraction_cap"
        out.cursor = {"pass": 1, "done_this_pass": 0, "passes_completed": 0}
        return out
    monkeypatch.setattr(bespoke, "run_rolling", fake_rolling)
    monkeypatch.setattr(bespoke, "load_companies", lambda: _companies(3))
    monkeypatch.setattr(bespoke, "load_state", lambda: {})
    monkeypatch.setattr(bespoke, "load_cursor", lambda: {})
    monkeypatch.setattr(fetch_budget, "remaining_today", lambda: daily_left)
    import profile_loader
    real = profile_loader.active()
    sources = dict(real["sources"], bespoke={"max_extractions_per_run": profile_cap} if profile_cap else {})
    monkeypatch.setattr(profile_loader, "active", lambda: {**real, "sources": sources})
    bespoke.fetch_all()
    assert seen["cap"] == expected
    assert fetch_budget.RUN["extractions"] == min(2, expected)
    out = capsys.readouterr().out
    assert "[Bespoke] extraction cap reached after" in out
    assert ("daily ceiling" in out) == (daily_left < (profile_cap or 60))


# ----------------------------------------------------------- caching, costs

class FakeMessages:
    def __init__(self, usage):
        self.kwargs, self.usage = None, usage

    def create(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text='{"score": 7}')],
                                     usage=self.usage, stop_reason="end_turn", stop_details=None)


def _anthropic(usage):
    messages = FakeMessages(usage)
    client = types.SimpleNamespace(messages=messages, with_options=lambda **k: client)
    return llm.AnthropicProvider(client=client), messages


def test_the_system_block_carries_cache_control_for_anthropic(monkeypatch):
    usage = types.SimpleNamespace(input_tokens=400, output_tokens=50, cache_creation_input_tokens=2100,
                                  cache_read_input_tokens=0)
    provider, messages = _anthropic(usage)
    monkeypatch.setattr(llm, "_provider", provider)
    text, got = llm.complete("claude-sonnet-4-6", "SYSTEM PROMPT", [{"role": "user", "content": "x"}], 10,
                             cache_system=True)
    assert messages.kwargs["system"] == [{"type": "text", "text": "SYSTEM PROMPT",
                                          "cache_control": {"type": "ephemeral"}}]
    assert got.input_tokens == 400 and got.cache_creation_input_tokens == 2100
    assert got.cache_read_input_tokens == 0 and got.total_input_tokens == 2500

    llm.complete("claude-sonnet-4-6", "SYSTEM PROMPT", [{"role": "user", "content": "x"}], 10)
    assert messages.kwargs["system"] == "SYSTEM PROMPT"                  # not asked: plain string


def test_the_stub_provider_never_sees_cache_control(llm_stub):
    stub = llm_stub(['{"a": 1}'])
    llm.complete("m", "SYSTEM PROMPT", [{"role": "user", "content": "x"}], 10, cache_system=True)
    assert stub.calls[0]["system"] == "SYSTEM PROMPT"
    assert "cache_system" not in stub.calls[0]["options"]


def test_the_scoring_status_and_extraction_calls_ask_for_the_cache():
    import inspect
    from sources import status_email
    assert "cache_system=True" in inspect.getsource(scorer._score_single)
    assert "cache_system=True" in inspect.getsource(status_email.call_claude)
    assert "cache_system=True" not in inspect.getsource(bespoke.extract_jobs)   # REV-09: under the minimum


def test_scoring_usage_with_cache_fields_flows_into_the_counters_and_cost(monkeypatch):
    usage = types.SimpleNamespace(input_tokens=300, output_tokens=120, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=2500)
    provider, messages = _anthropic(usage)
    monkeypatch.setattr(llm, "_provider", provider)
    monkeypatch.setattr(scorer, "USAGE", {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0})
    scorer._score_single({"title": "Director of IT", "company": "Acme"})
    assert isinstance(messages.kwargs["system"], list)                  # the scoring prompt is cached
    assert scorer.USAGE == {"calls": 1, "input": 300, "output": 120, "cache_write": 0, "cache_read": 2500}
    line = scorer.usage_line(model="claude-sonnet-4-6")
    assert ("1 scoring calls at claude-sonnet-4-6: input 2,800 tokens (uncached 300, cache write 0, "
            "cache read 2,500), output 120; cost $0.0034") in line


def test_cost_arithmetic_with_cache_tokens():
    cost = config.model_cost("claude-sonnet-4-6", 1_000_000, 100_000, 2_000_000, 3_000_000)
    assert cost == pytest.approx(3.00 + 1.50 + 2 * 3.75 + 3 * 0.30)
    assert config.model_cost("claude-sonnet-4-6", 1000, 10) == pytest.approx(0.003 + 0.00015)
    assert config.model_cost("claude-opus-5-5", 0, 0, 0, 1_000_000) == pytest.approx(0.20)    # 0.05x
    assert config.model_cost("x-unknown", 1, 1, 1, 1) is None
    assert set(config.MODEL_CACHE_PRICES) == set(config.MODEL_PRICES)


def test_calibrate_reports_cached_and_uncached_input():
    usage = {"calls": 10, "input": 4_000, "output": 2_000, "cache_write": 2_200, "cache_read": 19_800,
             "model": "claude-sonnet-4-6"}
    cost = calibrate.cost_summary(usage, per_day=40)
    assert cost["avg_input"] == 2_600
    assert cost["run"] == pytest.approx(config.model_cost("claude-sonnet-4-6", 4_000, 2_000, 2_200, 19_800))
    assert cost["uncached_cost"] == pytest.approx(config.model_cost("claude-sonnet-4-6", 26_000, 2_000))
    lines = calibrate.cost_lines(cost)
    assert lines[-1].startswith("  Input tokens this run: uncached 4,000, cache write 2,200, cache read 19,800")
    assert "without the cache this run would cost $0.1080" in lines[-1]
