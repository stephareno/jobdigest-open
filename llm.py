# llm.py — every model call goes through here (PUB-14)
#
#   text, usage = llm.complete(model, system, messages, max_tokens, json_mode=False)
#   data, usage = llm.complete_json(...)          # parsed object; one retry on malformed JSON
#
# The provider seam
# -----------------
# complete() hands the request to the active provider, an object with one
# method, complete(model, system, messages, max_tokens, json_mode, attachments,
# **options) -> (text, Usage). AnthropicProvider is the only one shipped; the
# client is created once, lazily, from ANTHROPIC_API_KEY (or the SDK's own
# ANTHROPIC_AUTH_TOKEN). Tests swap in a stub with use(); nothing else in the
# engine imports anthropic.
#
# An OpenAI-compatible provider (OpenAI, Azure, OpenRouter, a local server)
# would need:
#   - a base URL and its own key variable (say LLM_BASE_URL, LLM_API_KEY),
#     added to envfile.SECRETS so doctor and .env.example know them;
#   - system as the first {"role": "system"} message rather than a parameter;
#   - json_mode as response_format={"type": "json_object"} (Anthropic has no
#     such switch: the prompts ask for JSON and complete_json parses it);
#   - usage from prompt_tokens / completion_tokens, stop_reason from
#     finish_reason ("length" -> "max_tokens");
#   - no PDF attachments: setup --from-document reads a PDF only through
#     Anthropic, so another provider raises Unsupported and the user passes a
#     .txt or .md instead;
#   - the Anthropic-only options (thinking, output_config) dropped or refused;
#   - classify_error() taught its error types (401/403/402 abort, 429/5xx
#     and connection errors transient), and config.MODEL_PRICES its models.
#   - nothing for prompt caching: cache_system (REV-06) is an Anthropic
#     cache_control block. complete() passes it only to a provider whose
#     supports_cache is true, so an OpenAI-compatible seam ignores it (those
#     APIs cache long prefixes on their own, or not at all).
#
# Prompt caching (REV-06)
# -----------------------
# complete(..., cache_system=True) sends a string system prompt as one system
# block with cache_control {"type": "ephemeral"} (Anthropic's 5-minute cache),
# so calls that repeat it within five minutes read it at a tenth of the input
# price. The scoring and status-email prompts use it (REV-09: Bespoke
# extraction's 233-token prompt is under every minimum, so it no longer asks). The
# reply's cache_creation_input_tokens and cache_read_input_tokens land on
# Usage; input_tokens stays the uncached part, as the API reports it. A
# prompt shorter than the model's minimum cacheable length is simply not
# cached (both cache fields 0).
#
# REV-09: so every call that asks for the cache logs it. Once per run per
# call site (purpose, model, prompt): the system prompt's token count (the
# provider's count_system_tokens, Anthropic's free count_tokens endpoint) and
# the model's minimum (config.MODEL_CACHE_MIN_TOKENS):
#   [Cache] scoring call: system prompt 2,297 tokens, claude-sonnet-4-6 caches from 1,024: cacheable
# and per call the reply's two cache fields:
#   [Cache] scoring call: cache write 0, cache read 2,297 tokens
# Every model name comes from profile.yaml or config.py, so changing provider
# is this module plus those values.
#
# Errors (OPS-12)
# ---------------
# A billing, authentication or permission failure fails every call in the run
# the same way. complete() raises ApiAccessError for those, at every call site:
# a fetch run aborts red before anything is queued or marked seen, and a digest
# step (status tracking, the Applied seed, company profiles) stops and the
# digest goes on. Missing credentials are the same error. Rate limits, 5xx and
# timeouts are transient: the SDK retries them, then the original exception
# reaches the caller, whose fallback applies (is_transient tells them apart).

import base64
import json
import os
import threading
from dataclasses import dataclass


class ApiAccessError(RuntimeError):
    """The model API refused this run's credentials or account. Ends the run red."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


class Unsupported(ValueError):
    """The provider cannot carry this request (an attachment type, say)."""


@dataclass
class Usage:
    """What one call cost and how it ended. Token counts are the provider's own
    usage fields; calibrate's cost table and every per-step counter read them."""
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    refusal_category: str | None = None
    # REV-06: prompt-cache tokens, billed apart from input_tokens (which is
    # the uncached part only). Total input = all three.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input_tokens(self):
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


# --- error classification ------------------------------------------------------------

def _error_body(exc: Exception) -> dict:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body.get("error", body) if isinstance(body.get("error"), dict) else body
    return {}


def access_error_cause(exc: Exception) -> str:
    """A one-line cause if `exc` is a billing/auth/permission error, else ""."""
    if isinstance(exc, ApiAccessError):
        return exc.cause
    status = getattr(exc, "status_code", None)
    if status is None:
        return ""
    error = _error_body(exc)
    error_type = error.get("type", "")
    message = error.get("message") or str(exc)
    if status == 401 or error_type == "authentication_error":
        return f"authentication failed (401): {message}"
    if status == 403 or error_type == "permission_error":
        return f"permission denied (403): {message}"
    if status == 402 or error_type == "billing_error":
        return f"billing error ({status}): {message}"
    # No credit arrives as a plain 400 invalid_request_error; only its message
    # tells it apart from a malformed request.
    if status == 400 and "credit balance" in message.lower():
        return f"credit balance too low (400): {message}"
    return ""


def is_transient(exc: Exception) -> bool:
    """A call that may succeed on a later run: connection/timeout, 408, 409, 429 or 5xx.

    These are what the SDK already retries; reaching the caller means the
    retries ran out. A malformed request or reply is not transient.
    """
    try:
        import anthropic
        if isinstance(exc, anthropic.APIConnectionError):   # includes APITimeoutError
            return True
    except ImportError:
        pass
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status in (408, 409, 429) or status >= 500)


# --- the Anthropic provider ----------------------------------------------------------

def _credentials():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()


def system_blocks(system):
    """REV-06: a string system prompt as one cached Anthropic system block."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class AnthropicProvider:
    name = "anthropic"
    supports_cache = True

    def count_system_tokens(self, model, system):
        """REV-09: the system prompt's own tokens, as the API counts them: a
        count with it less a count without it (the same one-word message)."""
        messages = [{"role": "user", "content": "x"}]
        count = self.client().messages.count_tokens
        return (count(model=model, system=system, messages=messages).input_tokens
                - count(model=model, messages=messages).input_tokens)

    def __init__(self, client=None):
        self._client = client
        self._lock = threading.Lock()

    def available(self):
        return self._client is not None or bool(_credentials())

    def client(self):
        with self._lock:
            if self._client is None:
                if not _credentials():
                    raise ApiAccessError("no ANTHROPIC_API_KEY set")
                import anthropic
                key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
                self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
            return self._client

    def complete(self, model, system, messages, max_tokens, json_mode=False, attachments=None,
                 timeout=None, max_retries=None, cache_system=False, **options):
        # json_mode changes nothing here: the prompts ask for JSON themselves.
        client = self.client()
        if max_retries is not None:
            client = client.with_options(max_retries=max_retries)
        kwargs = {"model": model, "max_tokens": max_tokens, "messages": _with_attachments(messages, attachments)}
        if system is not None:
            kwargs["system"] = system_blocks(system) if cache_system and isinstance(system, str) else system
        if timeout is not None:
            kwargs["timeout"] = timeout
        msg = client.messages.create(**kwargs, **options)
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", "text") == "text" and hasattr(b, "text"))
        usage = getattr(msg, "usage", None)
        details = getattr(msg, "stop_details", None)
        return text, Usage(model=model,
                           input_tokens=getattr(usage, "input_tokens", 0) or 0,
                           output_tokens=getattr(usage, "output_tokens", 0) or 0,
                           stop_reason=getattr(msg, "stop_reason", "") or "",
                           refusal_category=getattr(details, "category", None),
                           cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                           cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0)


def _with_attachments(messages, attachments):
    """Attachments go first in the last message's content, as Anthropic
    document blocks. Only PDF is carried."""
    if not attachments:
        return messages
    blocks = []
    for a in attachments:
        if a.get("media_type") != "application/pdf":
            raise Unsupported(f"attachment type {a.get('media_type')!r} is not supported")
        blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                      "data": base64.standard_b64encode(a["data"]).decode()}})
    *head, last = messages
    content = last["content"]
    content = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    return [*head, {**last, "content": blocks + content}]


# --- the seam ------------------------------------------------------------------------

_provider = AnthropicProvider()


def use(provider):
    """Make `provider` the one every call goes through; returns the previous one."""
    global _provider
    previous, _provider = _provider, provider
    return previous


def provider():
    return _provider


def available():
    """True when a call could be made: the provider has credentials. A step
    that can skip itself (Bespoke, company profiles) checks this first."""
    check = getattr(_provider, "available", None)
    return check() if check else True


def complete(model, system, messages, max_tokens, json_mode=False, attachments=None,
             purpose=None, **options):
    """One model call -> (text, Usage). `messages` is the Anthropic shape
    ([{"role": "user", "content": ...}]); `system` may be None. `options` are
    provider extras (timeout, max_retries, thinking, output_config).

    A billing/auth/permission error, or missing credentials, raises
    ApiAccessError ("<purpose> failed: <cause>" when a purpose is given); any
    other error is re-raised as it came. cache_system=True asks for the
    system prompt to be cached (REV-06); a provider without supports_cache
    never sees the option."""
    if not getattr(_provider, "supports_cache", False):
        options.pop("cache_system", None)
    cached = bool(options.get("cache_system")) and isinstance(system, str)
    if cached:
        log_cache_site(purpose, model, system)
    try:
        text, usage = _provider.complete(model, system, messages, max_tokens, json_mode=json_mode,
                                         attachments=attachments, **options)
        if cached:
            print(cache_usage_line(purpose, usage))
        return text, usage
    except Exception as e:
        cause = access_error_cause(e)
        if not cause:
            raise
        raise ApiAccessError(f"{purpose} failed: {cause}" if purpose else cause) from e


# --- prompt-cache logging (REV-09) ----------------------------------------------------

_CACHE_SITES_LOGGED = set()


def reset_cache_log():
    """Forget which call sites were logged (a new run in the same process)."""
    _CACHE_SITES_LOGGED.clear()


def cache_minimum(model):
    import config
    return config.MODEL_CACHE_MIN_TOKENS.get(model)


def system_prompt_tokens(model, system):
    """(tokens, note): the provider's count, or (None, why not)."""
    count = getattr(_provider, "count_system_tokens", None)
    if count is None:
        return None, "not counted: the provider has no token counter"
    try:
        return int(count(model, system)), ""
    except Exception as e:                        # noqa: BLE001 - a log line must never stop a call
        return None, f"not counted: {type(e).__name__}: {str(e)[:120]}"


def cache_site_line(purpose, model, tokens, minimum, note=""):
    site = purpose or "model call"
    size = f"{tokens:,} tokens" if tokens is not None else note
    if minimum is None:
        return f"[Cache] {site}: system prompt {size}, {model} minimum unknown"
    verdict = ("cannot tell" if tokens is None else
               "cacheable" if tokens >= minimum else "below the minimum, never cached")
    return f"[Cache] {site}: system prompt {size}, {model} caches from {minimum:,}: {verdict}"


def log_cache_site(purpose, model, system):
    """Print cache_site_line once per run for this (purpose, model, prompt)."""
    key = (purpose, model, hash(system))
    if key in _CACHE_SITES_LOGGED:
        return
    _CACHE_SITES_LOGGED.add(key)
    tokens, note = system_prompt_tokens(model, system)
    print(cache_site_line(purpose, model, tokens, cache_minimum(model), note))


def cache_usage_line(purpose, usage):
    return (f"[Cache] {purpose or 'model call'}: cache write {usage.cache_creation_input_tokens:,}, "
            f"cache read {usage.cache_read_input_tokens:,} tokens")


def parse_json(text):
    """The JSON object in a reply. The model sometimes wraps it in a fence or
    puts a line of prose before it; read from the first "{" to the last "}"."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(text[start:end + 1])


def complete_json(model, system, messages, max_tokens, attachments=None, purpose=None,
                  retries=1, on_usage=None, validate=None, **options):
    """complete() in JSON mode, parsed by parse_json -> (object, Usage of the
    last call). A malformed reply is asked again, `retries` times, before its
    ValueError reaches the caller. validate(obj), when given, returns the
    object to use or raises ValueError: a reply it rejects counts as
    malformed too (REV-05). on_usage(usage) sees every call, retries
    included, so token counts stay true."""
    for attempt in range(retries + 1):
        text, usage = complete(model, system, messages, max_tokens, json_mode=True,
                               attachments=attachments, purpose=purpose, **options)
        if on_usage:
            on_usage(usage)
        try:
            obj = parse_json(text)
            return (validate(obj) if validate else obj), usage
        except ValueError as e:
            if attempt == retries:
                raise
            print(f"[LLM] {purpose or 'model call'}: malformed JSON reply ({e}), asking once more")
