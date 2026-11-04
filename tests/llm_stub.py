"""PUB-14: the stub model provider every test injects into llm.py.

    stub = StubProvider(lambda call: '{"score": 7}')
    llm.use(stub)                        # or the llm_stub fixture in conftest
    ...
    assert stub.calls[0]["model"] == "claude-sonnet-4-6" and stub.calls[0]["json_mode"]

`respond(call)` gets one recorded call and returns the reply text, a
(text, usage) pair (usage a dict of Usage fields), or raises. A list of
replies is answered in order. Each call is recorded as a dict: model,
system, messages, max_tokens, json_mode, attachments, options, and
`content` (the last message's text, blocks joined) for easy matching."""

import llm


def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


class StubProvider:
    name = "stub"

    def __init__(self, respond=None, usage=None, available=True):
        if isinstance(respond, (list, tuple)):
            replies = list(respond)
            respond = lambda call: replies.pop(0)       # noqa: E731
        self.respond = respond or (lambda call: "")
        self.usage = usage or {"input_tokens": 100, "output_tokens": 20}
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def record(self, call):
        self.calls.append(call)

    def complete(self, model, system, messages, max_tokens, json_mode=False, attachments=None, **options):
        call = {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens,
                "json_mode": json_mode, "attachments": attachments, "options": options,
                "content": _text(messages[-1]["content"])}
        self.record(call)
        reply = self.respond(call)
        text, usage = reply if isinstance(reply, tuple) else (reply, {})
        return text, llm.Usage(model=model, **{**self.usage, **usage})


class Unstubbed:
    """The provider every test starts with: any call it gets is a test that
    forgot its stub, never a request to the real API."""
    name = "unstubbed"

    def available(self):
        return True

    def complete(self, model, *args, **kwargs):
        raise AssertionError(f"model call to {model} with no stub provider (use the llm_stub fixture)")
