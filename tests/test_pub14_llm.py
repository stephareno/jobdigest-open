"""PUB-14: every model call goes through llm.py.

Covers: no engine module touches the Anthropic SDK but llm.py; the Anthropic
provider sends the same request each call site sent before (system left out
when None, per-call timeout, max_retries, thinking, PDF attachments) and reads
text, usage and stop reason back; the OPS-12 classification applied at every
call site (billing/auth abort, missing credentials abort, transient re-raised
as it came); complete_json's one retry on malformed JSON and its usage
counting; and which model and JSON mode each call site asks for.
"""

import json
import os
import re
import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GOOGLE_SHEET_ID", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")

import anthropic  # noqa: E402

import llm  # noqa: E402


def _api_error(cls, status, message, error_type="invalid_request_error"):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=request),
               body={"type": "error", "error": {"type": error_type, "message": message}})


CREDIT = _api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API.")
OVERLOADED = _api_error(anthropic.InternalServerError, 529, "Overloaded", "overloaded_error")


def _raises(error):
    def respond(call):
        raise error
    return respond


# --- the seam --------------------------------------------------------------------------

def test_no_module_but_llm_touches_the_sdk():
    """Every model call goes through llm.complete: no other module imports
    anthropic or calls messages.create (tests excepted)."""
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in ("tests", ".venv", "venv") or rel.name == "llm.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(import anthropic|from anthropic\b)|messages\.create\(", text, re.M):
            offenders.append(str(rel))
    assert offenders == []


class RecordingClient:
    """A fake anthropic.Anthropic for the provider's own request shape."""

    def __init__(self, content=None, stop_reason="end_turn"):
        self.requests, self.options = [], []
        self.messages = self
        self.content = content or [types.SimpleNamespace(type="text", text='{"ok": true}')]
        self.stop_reason = stop_reason

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def create(self, **kw):
        self.requests.append(kw)
        return types.SimpleNamespace(content=self.content, stop_reason=self.stop_reason,
                                     usage=types.SimpleNamespace(input_tokens=321, output_tokens=12))


def test_the_anthropic_provider_sends_what_the_call_sites_sent():
    client = RecordingClient()
    provider = llm.AnthropicProvider(client=client)
    text, usage = provider.complete("claude-haiku-4-5", None, [{"role": "user", "content": "hi"}], 50)
    assert client.requests[-1] == {"model": "claude-haiku-4-5", "max_tokens": 50,
                                   "messages": [{"role": "user", "content": "hi"}]}     # no system key
    assert (text, usage.input_tokens, usage.output_tokens, usage.stop_reason) == ('{"ok": true}', 321, 12, "end_turn")

    provider.complete("m", "sys", [{"role": "user", "content": "x"}], 10, json_mode=True,
                      timeout=30.0, max_retries=1, thinking={"type": "adaptive"})
    assert client.requests[-1] == {"model": "m", "max_tokens": 10, "system": "sys", "timeout": 30.0,
                                   "thinking": {"type": "adaptive"},
                                   "messages": [{"role": "user", "content": "x"}]}
    assert client.options == [{"max_retries": 1}]


def test_reply_text_skips_thinking_blocks():
    client = RecordingClient(content=[types.SimpleNamespace(type="thinking", thinking="..."),
                                      types.SimpleNamespace(type="text", text="a"),
                                      types.SimpleNamespace(type="text", text="b")])
    text, _ = llm.AnthropicProvider(client=client).complete("m", None, [{"role": "user", "content": "x"}], 10)
    assert text == "ab"


def test_a_pdf_goes_first_as_a_document_block_and_other_types_are_refused():
    messages = [{"role": "user", "content": [{"type": "text", "text": "PROMPT"}]}]
    sent = llm._with_attachments(messages, [{"media_type": "application/pdf", "data": b"%PDF"}])
    assert sent[0]["content"][0] == {"type": "document", "source": {"type": "base64",
                                     "media_type": "application/pdf", "data": "JVBERg=="}}
    assert sent[0]["content"][1] == {"type": "text", "text": "PROMPT"}
    with pytest.raises(llm.Unsupported):
        llm._with_attachments(messages, [{"media_type": "image/png", "data": b""}])


def test_missing_credentials_are_an_access_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm, "_provider", llm.AnthropicProvider())
    assert not llm.available()
    with pytest.raises(llm.ApiAccessError, match="scoring call failed: no ANTHROPIC_API_KEY set"):
        llm.complete("m", None, [{"role": "user", "content": "x"}], 1, purpose="scoring call")


# --- errors ----------------------------------------------------------------------------

def test_access_errors_abort_and_transient_ones_come_through_as_they_were(llm_stub):
    llm_stub(_raises(CREDIT))
    with pytest.raises(llm.ApiAccessError, match="^weekly summary call failed: credit balance too low"):
        llm.complete("m", None, [{"role": "user", "content": "x"}], 1, purpose="weekly summary call")
    llm_stub(_raises(OVERLOADED))
    with pytest.raises(anthropic.InternalServerError) as info:
        llm.complete("m", None, [{"role": "user", "content": "x"}], 1)
    assert llm.is_transient(info.value)


def test_the_applied_seed_stops_on_an_access_error(monkeypatch, llm_stub):
    from sources import applied_doc
    llm_stub(_raises(CREDIT))
    monkeypatch.setattr(applied_doc.sheets, "get_index_rows", lambda: [])
    monkeypatch.setattr(applied_doc, "read_entries", lambda: [
        {"heading": "1 ???", "body": "x"}, {"heading": "2 ???", "body": "y"}])
    with pytest.raises(llm.ApiAccessError, match="Applied-seed extraction failed"):
        applied_doc.seed_from_doc(dry_run=True)


def test_company_profiles_stop_on_an_access_error(llm_stub, tmp_path):
    from tests.test_del14_company_profiles import ROUTES, TODAY, _factory, _job, _sites
    import profiles
    llm_stub(_raises(CREDIT))
    with pytest.raises(llm.ApiAccessError, match="company profile call failed"):
        profiles.enrich_blurbs([_job()], cache_path=tmp_path / "c.json", today=TODAY,
                               sites=_sites(), http_factory=_factory(ROUTES))


def test_setup_from_document_stops_on_an_access_error_and_the_cli_exits_red(monkeypatch, llm_stub,
                                                                            tmp_path, capsys):
    import cli
    import setup_document
    doc = tmp_path / "resume.txt"
    doc.write_text("resume")
    llm_stub(_raises(CREDIT))
    with pytest.raises(llm.ApiAccessError):
        setup_document.propose(str(doc), "claude-sonnet-4-6")

    def calibrate_main(argv):
        llm.complete("m", None, [{"role": "user", "content": "x"}], 1, purpose="scoring call")
    import calibrate
    monkeypatch.setattr(calibrate, "main", calibrate_main)
    assert cli.early(["calibrate", "--dir", "examples/postings"]) == 1
    assert "::error title=Anthropic API access failed::scoring call failed: credit balance" in capsys.readouterr().out


def test_doctor_names_the_access_cause(monkeypatch, llm_stub):
    import doctor
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    llm_stub(_raises(CREDIT))
    r = doctor.check_anthropic(None)
    assert r["status"] == doctor.FAIL and "refused (credit balance too low (400)" in r["reason"]


def test_status_email_refuses_a_cut_or_declined_reply(llm_stub):
    from sources import status_email as se
    llm_stub(lambda call: ('{"status": "none"}', {"stop_reason": "max_tokens"}))
    with pytest.raises(ValueError, match="cut at max_tokens"):
        se.call_claude("x")
    llm_stub(lambda call: ("", {"stop_reason": "refusal", "refusal_category": "cyber"}))
    with pytest.raises(ValueError, match="refusal, category cyber"):
        se.call_claude("x")


# --- complete_json ---------------------------------------------------------------------

GOOD = {"score": 7, "rationale": "x", "is_insurance": False, "is_cool_company_wrong_role": False,
        "function_fit": "leads_department", "work_model": "Remote", "city": None, "state": None,
        "hiring_company": "Acme"}


def test_a_malformed_scoring_reply_is_asked_once_more_and_both_calls_are_counted(monkeypatch, llm_stub):
    import scorer
    monkeypatch.setattr(scorer, "USAGE", {"calls": 0, "input": 0, "output": 0})
    stub = llm_stub(["Sure! Here is the score.", "```json\n" + json.dumps(GOOD) + "\n```"])
    assert scorer._score_single({"title": "Director of IT", "company": "Acme"})["score"] == 7
    assert len(stub.calls) == 2 and stub.calls[0] == stub.calls[1]
    assert scorer.USAGE == {"calls": 2, "input": 200, "output": 40}


def test_a_second_malformed_reply_is_a_scoring_error_not_a_deferral(llm_stub):
    import scorer
    stub = llm_stub(lambda call: "no json here")
    result = scorer._score_single({"title": "Director of IT", "company": "Acme"})
    assert result["rationale"] == scorer.SCORING_ERROR and "scoring_error" not in result
    assert len(stub.calls) == 2


# --- what each call site asks for ------------------------------------------------------

def test_each_call_site_names_its_model_and_json_mode(monkeypatch, llm_stub):
    import config
    import profile_loader
    import scorer
    import setup_document
    import weekly_summary
    import websites
    from sources import applied_doc, bespoke

    stub = llm_stub(lambda call: json.dumps({**GOOD, "company": "Acme", "title": "VP IT", "applied_date": "",
                                            "jobs": [], "website": "acme.com"}))
    scorer._score_single({"title": "Director of IT", "company": "Acme"})
    weekly_summary.read_declines_with_claude([{"company": "A", "title": "T", "notes": "n"}])
    applied_doc.claude_extract("1 Acme - VP IT", "Applied 1/2/2026")
    bespoke.extract_jobs("Acme", "<html></html>", "https://acme.com/careers")
    websites.ModelGuesser(config.PROFILE_MODEL).guess("Acme")
    setup_document.propose(str(ROOT / "examples" / "postings" / "02-strong-fit-remote.md"), "claude-sonnet-4-6")

    asked = [(c["model"], c["json_mode"]) for c in stub.calls]
    assert asked == [
        (scorer.MODEL, True),
        (weekly_summary.READING_MODEL, False),
        (applied_doc.MODEL, True),
        (config.BESPOKE_EXTRACT_MODEL, True),
        (config.PROFILE_MODEL, True),
        ("claude-sonnet-4-6", True),
    ]
    assert scorer.MODEL == profile_loader.active()["scoring"]["model"]
    assert stub.calls[1]["system"] is None and stub.calls[0]["system"] == scorer.SYSTEM_PROMPT
    assert stub.calls[3]["options"] == {"timeout": config.BESPOKE_EXTRACT_TIMEOUT}
