"""REV-09: prompt-cache logging, and what no cached system prompt may hold.

Covers: a call that asks for the cache logs the system prompt's token count
against the model's minimum once per run per call site, and its cache write
and read tokens on every call; a provider without caching logs nothing; a
counter failure never stops the call; the minimum table holds the brief's
figures; Bespoke extraction no longer asks for the cache (its prompt is under
every minimum); the scoring and status-email system prompts carry no date, run
id or posting text (the same text for two different postings). No network.
"""

import json

import pytest

import config
import llm
import scorer
from sources import bespoke, status_email
from tests.llm_stub import StubProvider


class CachingStub(StubProvider):
    supports_cache = True

    def __init__(self, *a, tokens=2531, fail=None, **kw):
        super().__init__(*a, **kw)
        self.tokens, self.fail, self.counted = tokens, fail, []

    def count_system_tokens(self, model, system):
        self.counted.append((model, system))
        if self.fail:
            raise self.fail
        return self.tokens


@pytest.fixture
def caching(monkeypatch):
    llm.reset_cache_log()

    def install(**kw):
        stub = CachingStub(**kw)
        monkeypatch.setattr(llm, "_provider", stub)
        return stub
    yield install
    llm.reset_cache_log()


def _call(purpose="scoring call", model="claude-sonnet-4-6", system="SYSTEM", **kw):
    return llm.complete(model, system, [{"role": "user", "content": "x"}], 10, purpose=purpose,
                        cache_system=True, **kw)


def test_the_site_line_once_per_run_and_the_usage_line_every_call(caching, capsys):
    stub = caching(usage={"input_tokens": 400, "output_tokens": 20, "cache_creation_input_tokens": 2532})
    _call()
    stub.usage = {"input_tokens": 400, "output_tokens": 20, "cache_read_input_tokens": 2532}
    _call()
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "[Cache] scoring call: system prompt 2,531 tokens, claude-sonnet-4-6 caches from 1,024: cacheable",
        "[Cache] scoring call: cache write 2,532, cache read 0 tokens",
        "[Cache] scoring call: cache write 0, cache read 2,532 tokens",
    ]
    assert len(stub.counted) == 1                                 # counted once, not per call


def test_each_site_and_model_is_logged_on_its_own(caching, capsys):
    caching(tokens=233)
    _call("Bespoke extraction", "claude-haiku-4-5")
    _call("status email call", "claude-haiku-5-5")
    _call("mystery call", "claude-new-model")
    lines = [l for l in capsys.readouterr().out.splitlines() if "system prompt" in l]
    assert lines == [
        "[Cache] Bespoke extraction: system prompt 233 tokens, claude-haiku-4-5 caches from 4,096: "
        "below the minimum, never cached",
        "[Cache] status email call: system prompt 233 tokens, claude-haiku-5-5 caches from 512: "
        "below the minimum, never cached",
        "[Cache] mystery call: system prompt 233 tokens, claude-new-model minimum unknown",
    ]


def test_reset_logs_the_site_again(caching, capsys):
    caching()
    _call()
    llm.reset_cache_log()
    _call()
    assert sum("system prompt" in l for l in capsys.readouterr().out.splitlines()) == 2


def test_a_counter_failure_never_stops_the_call(caching, capsys):
    caching(fail=RuntimeError("count endpoint down"))
    text, _ = _call()
    assert text == ""
    out = capsys.readouterr().out
    assert ("[Cache] scoring call: system prompt not counted: RuntimeError: count endpoint down, "
            "claude-sonnet-4-6 caches from 1,024: cannot tell") in out
    assert "cache write 0, cache read 0 tokens" in out


def test_no_logging_without_the_cache_or_a_caching_provider(caching, llm_stub, capsys):
    stub = caching()
    llm.complete("claude-sonnet-4-6", "S", [{"role": "user", "content": "x"}], 10)   # no cache_system
    assert capsys.readouterr().out == "" and stub.counted == []
    llm_stub()                                                    # a provider without supports_cache
    _call()
    assert capsys.readouterr().out == ""


def test_the_minimum_table_holds_the_brief_figures():
    assert config.MODEL_CACHE_MIN_TOKENS == {"claude-sonnet-4-6": 1024, "claude-haiku-4-5": 4096,
                                             "claude-haiku-5-5": 512}
    assert set(config.MODEL_CACHE_MIN_TOKENS) <= set(config.MODEL_PRICES)


def test_the_anthropic_counter_subtracts_the_message(monkeypatch):
    seen = []

    class Count:
        def __init__(self, n):
            self.input_tokens = n

    class Messages:
        def count_tokens(self, **kw):
            seen.append(kw)
            return Count(240 if "system" in kw else 7)

    class Client:
        messages = Messages()
    provider = llm.AnthropicProvider(client=Client())
    assert provider.count_system_tokens("claude-haiku-4-5", "SYS") == 233
    assert seen[0]["system"] == "SYS" and "system" not in seen[1]


def test_bespoke_extraction_no_longer_asks_for_the_cache(caching, capsys):
    stub = caching(tokens=233)
    stub.respond = lambda call: json.dumps({"jobs": []})
    bespoke.extract_jobs("Northwind Robotics", "<html><body>Careers</body></html>",
                         "https://northwind-robotics.example/careers")
    assert "cache_system" not in stub.calls[0]["options"]
    assert "[Cache]" not in capsys.readouterr().out


def test_the_cached_system_prompts_hold_nothing_that_changes_per_call(llm_stub):
    """Step 5: the text before each breakpoint is the same for any posting,
    email or run; the posting goes in the user message."""
    reply = json.dumps({"score": 6, "rationale": {"company": "c", "role": "r", "culture": "x."},
                        "criteria_hits": [], "hard_block_industry": None, "function_fit": "leads_department",
                        "work_model": "Remote", "city": None, "state": None, "hiring_company": None,
                        "website": None, "is_cool_company_wrong_role": False})
    stub = llm_stub([reply, reply])
    scorer._score_single({"title": "Director of IT", "company": "Initech", "description": "Leads IT. RUN-1"})
    scorer._score_single({"title": "VP, Technology", "company": "Globex", "description": "Owns infra. RUN-2"})
    first, second = stub.calls[0]["system"], stub.calls[1]["system"]
    assert first == second == scorer.SYSTEM_PROMPT
    for system in (scorer.SYSTEM_PROMPT, status_email.SYSTEM_PROMPT):
        for marker in ("Initech", "Globex", "RUN-1", "{{", "GITHUB_RUN"):
            assert marker not in system
        import re
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", system)                 # no ISO date
