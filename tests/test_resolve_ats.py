"""Tests for tools/resolve_ats.py using mocked httpx transports.

Covers: a Greenhouse hit, a Lever hit, an all-404 unresolved company, and the
single retry on a 5xx response. No network access.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from resolve_ats import (  # noqa: E402
    HostThrottle,
    board_matches_company,
    probe,
    resolve_company,
    slug_candidates,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _resolve(handler, name: str):
    async def inner():
        async with _client(handler) as client:
            return await resolve_company(
                client, name, HostThrottle(base_delay=0), asyncio.Semaphore(8)
            )

    return asyncio.run(inner())


def test_greenhouse_hit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io" and "/boards/huntress/" in request.url.path:
            return httpx.Response(200, json={"jobs": [{"title": "Director of IT",
                                                       "content": "Huntress protects small businesses."}]})
        return httpx.Response(404)

    result = _resolve(handler, "Huntress")
    assert result is not None
    assert result["ats"] == "greenhouse"
    assert result["slug"] == "huntress"
    assert result["board_url"] == (
        "https://boards-api.greenhouse.io/v1/boards/huntress/jobs?content=true"
    )


def test_lever_hit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co" and "/v0/postings/shieldai" in request.url.path:
            return httpx.Response(200, json=[{"text": "VP, Information Technology",
                                              "description": "Shield AI builds Hivemind."}])
        return httpx.Response(404)

    result = _resolve(handler, "Shield AI")
    assert result is not None
    assert result["ats"] == "lever"
    assert result["slug"] == "shieldai"


def test_all_404_unresolved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    assert _resolve(handler, "SpaceX") is None


def test_first_word_impostor_rejected() -> None:
    """A first-word slug owned by an unrelated company must not resolve."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co" and request.url.path == "/v0/postings/blue":
            return httpx.Response(200, json=[{
                "text": "Senior Data Engineer",
                "description": "Join Blue Analytics, a LATAM data consultancy.",
            }])
        return httpx.Response(404)

    assert _resolve(handler, "Blue Origin") is None


def test_first_word_hit_with_matching_text_accepted() -> None:
    """A first-word slug is fine when the board's text names the company."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/applied"):
            return httpx.Response(200, json={"jobs": [{
                "title": "Director of IT",
                "descriptionPlain": "Applied Intuition builds autonomy software.",
            }]})
        return httpx.Response(404)

    result = _resolve(handler, "Applied Intuition")
    assert result is not None
    assert result["ats"] == "ashby"
    assert result["slug"] == "applied"


def test_full_name_slug_needs_no_verification() -> None:
    """A slug matching the full name is trusted even if the JD text is silent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.ashbyhq.com" and request.url.path.endswith("/base-power"):
            return httpx.Response(200, json={"jobs": [{
                "title": "Journeyman Electrician",
                "descriptionPlain": "We install home batteries.",
            }]})
        return httpx.Response(404)

    result = _resolve(handler, "Base Power")
    assert result is not None
    assert result["slug"] == "base-power"


def test_board_matches_company_normalizes_ampersand() -> None:
    data = {"jobs": [{"title": "IT Lead", "descriptionPlain": "About Weights & Biases: we build ML tools."}]}
    assert board_matches_company("Weights and Biases", "ashby", data)


def test_5xx_retried_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"jobs": [{"title": "CIO"}]})

    url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"

    async def inner():
        async with _client(handler) as client:
            return await probe(
                client,
                url,
                lambda data: bool(data.get("jobs")),
                HostThrottle(base_delay=0),
                asyncio.Semaphore(8),
            )

    assert asyncio.run(inner()) == {"jobs": [{"title": "CIO"}]}
    assert calls["n"] == 2


def test_5xx_not_retried_twice() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    url = "https://api.lever.co/v0/postings/acme?mode=json"

    async def inner():
        async with _client(handler) as client:
            return await probe(
                client,
                url,
                lambda data: bool(data),
                HostThrottle(base_delay=0),
                asyncio.Semaphore(8),
            )

    assert asyncio.run(inner()) is None
    assert calls["n"] == 2


def test_empty_jobs_array_is_a_miss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404)

    assert _resolve(handler, "GhostTown") is None


def test_slug_candidates_variants() -> None:
    slugs = [c.slug for c in slug_candidates("Shield AI")]
    assert slugs == ["shieldai", "shield-ai", "shield"]
    assert "varda-space" in [c.slug for c in slug_candidates("Varda Space Industries")]
    assert [c.slug for c in slug_candidates("Q-CTRL")] == ["qctrl", "q-ctrl", "q"]
    assert [c.slug for c in slug_candidates("1Password")] == ["1password"]


def test_slug_candidates_verification_flags() -> None:
    by_slug = {c.slug: c.needs_verification for c in slug_candidates("Blue Origin")}
    assert by_slug["blueorigin"] is False
    assert by_slug["blue-origin"] is False
    assert by_slug["blue"] is True
    # Suffix-stripped variants are full-name-derived, so they stay trusted.
    mistral = {c.slug: c.needs_verification for c in slug_candidates("Mistral AI")}
    assert mistral["mistral"] is False


def test_one_word_name_is_not_guessed_on_a_shared_namespace() -> None:
    """A one-word company name is a common word on long-tail vendors.

    Observed 2026-09-16: "Teleport" resolved to a German telecom's Personio
    board, whose subcompany field even contained the name. Slug guessing no
    longer accepts it; only the company's own careers page can.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "teleport.jobs.personio.de":
            return httpx.Response(200, json=[{"name": "Sachbearbeiter Auftragsmanagement (m/w/d)",
                                              "department": "Vertrieb", "subcompany": "Teleport GmbH"}])
        return httpx.Response(404)

    assert _resolve(handler, "Teleport") is None


def test_one_word_name_on_a_per_company_vendor_stays_trusted() -> None:
    """Rippling is not a shared small-tenant namespace; a whole-name slug hit stands."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.rippling.com" and "/board/zerofox/" in request.url.path:
            return httpx.Response(200, json=[{"name": "IT Director", "uuid": "1"}])
        return httpx.Response(404)

    result = _resolve(handler, "ZeroFox")
    assert result is not None and result["ats"] == "rippling"


def test_invalid_hostname_is_a_miss_not_a_crash() -> None:
    """A non-ASCII slug builds an invalid IDNA host; the probe must just miss."""
    async def inner():
        async with _client(lambda request: httpx.Response(404)) as client:
            return await probe(client, "https://data\u00b2.recruitee.com/api/offers/",
                               lambda d: True, HostThrottle(base_delay=0), asyncio.Semaphore(1))

    assert asyncio.run(inner()) is None
