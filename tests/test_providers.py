import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.openserp import OpenSerpProvider
from app.services.search import SearxngProvider


def run(coro):
    return asyncio.run(coro)


def resp(status_code: int, payload) -> httpx.Response:
    return httpx.Response(
        status_code, json=payload, request=httpx.Request("GET", "https://x/")
    )


# --- SearxngProvider -------------------------------------------------------

SEARXNG_SEARCH_PAYLOAD = {
    "query": "raspberry pi gpio",
    "results": [
        {
            "url": "https://pinout.xyz/",
            "title": "Raspberry Pi GPIO Pinout",
            "content": "Learn about the GPIO pins.",
            "engines": ["brave"],
            "engine": "brave",
            "score": 1.0,
            "publishedDate": None,
        },
        {
            "url": "https://example.com/gpio",
            "title": "GPIO guide",
            "content": "A guide.",
            "engines": ["wikipedia"],
            "score": 0.5,
        },
    ],
    "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["mojeek", "access denied"], ["startpage", "CAPTCHA"]],
}

SEARXNG_CONFIG_PAYLOAD = {
    "engines": [
        {"name": "brave", "enabled": True},
        {"name": "wikipedia", "enabled": True},
        {"name": "duckduckgo", "enabled": True},
        {"name": "mojeek", "enabled": True},
        {"name": "startpage", "enabled": True},
        {"name": "arxiv", "enabled": True},  # enabled but silent in this probe -> unknown
        {"name": "google", "enabled": False},  # disabled on this instance -> excluded entirely
    ]
}


def test_searxng_search_parses_results_and_rank():
    mock_get = AsyncMock(return_value=resp(200, SEARXNG_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(SearxngProvider().search("raspberry pi gpio"))
    assert response.status == "ok"
    assert [r.url for r in response.results] == ["https://pinout.xyz/", "https://example.com/gpio"]
    assert response.results[0].rank == 1
    assert response.results[0].engines == ["brave"]
    assert response.results[1].rank == 2


def test_searxng_search_http_error_is_isolated_not_raised():
    mock_get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(SearxngProvider().search("q"))
    assert response.status == "failed"
    assert response.results == []
    assert "refused" in response.error


def test_searxng_health_classifies_engines():
    responses = [resp(200, SEARXNG_CONFIG_PAYLOAD), resp(200, SEARXNG_SEARCH_PAYLOAD)]
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        health = run(SearxngProvider().health())
    by_name = {e.name: e for e in health.engines}
    assert health.reachable is True
    assert by_name["brave"].status == "ok"
    assert by_name["duckduckgo"].status == "degraded"
    assert by_name["duckduckgo"].reason == "CAPTCHA"
    assert by_name["mojeek"].reason == "blocked"  # "access denied" -> classified as blocked
    assert by_name["arxiv"].status == "unknown"
    assert "google" not in by_name  # disabled engines are not reported at all


def test_searxng_health_unreachable_is_degraded_not_crashed():
    mock_get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient.get", mock_get):
        health = run(SearxngProvider().health())
    assert health.reachable is False
    assert health.engines == []
    assert "refused" in health.last_error


# --- OpenSerpProvider --------------------------------------------------------

MEGA_SEARCH_PAYLOAD = {
    "query": {"text": "raspberry pi gpio", "engines_requested": ["bing", "duckduckgo", "google", "yandex", "ecosia"]},
    "meta": {
        "engines_responded": ["duckduckgo", "bing"],
        "engines_failed": ["ecosia", "google", "yandex"],
        "engine_errors": [
            {"engine": "ecosia", "error": "blocked", "message": "blocked"},
            {"engine": "google", "error": "captcha_detected", "message": "captcha detected"},
            {"engine": "yandex", "error": "captcha_detected", "message": "captcha detected"},
        ],
    },
    "results": [
        {
            "id": "s_1",
            "rank": 1,
            "type": "ad",
            "title": "Sponsored",
            "url": "https://ads.example/",
            "domain": "ads.example",
            "engine": "duckduckgo",
            "position": {"absolute": 1},
        },
        {
            "id": "s_2",
            "rank": 1,
            "type": "organic",
            "title": "Raspberry Pi GPIO Pinout",
            "snippet": "Learn about the GPIO pins.",
            "url": "https://pinout.xyz/",
            "domain": "pinout.xyz",
            "engine": "bing",
            "position": {"absolute": 1},
        },
    ],
}


def test_openserp_search_skips_ads_and_maps_warnings():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("raspberry pi gpio"))
    assert response.status == "ok"
    assert [r.url for r in response.results] == ["https://pinout.xyz/"]
    assert response.results[0].engines == ["bing"]
    assert sorted(response.warnings) == [
        "ecosia: blocked",
        "google: CAPTCHA",
        "yandex: CAPTCHA",
    ]


def test_openserp_search_http_error_is_isolated():
    mock_get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("q"))
    assert response.status == "failed"
    assert response.results == []


def test_openserp_health_probes_real_engine_state():
    health_ok = resp(200, {"status": "healthy"})
    mega_probe = resp(200, MEGA_SEARCH_PAYLOAD)
    mock_get = AsyncMock(side_effect=[health_ok, mega_probe])
    with patch("httpx.AsyncClient.get", mock_get):
        health = run(OpenSerpProvider().health())
    by_name = {e.name: e for e in health.engines}
    assert health.reachable is True
    assert by_name["bing"].status == "ok"
    assert by_name["google"].status == "degraded"
    assert by_name["google"].reason == "CAPTCHA"
    assert by_name["ecosia"].reason == "blocked"


def test_openserp_health_degraded_overall_still_reports_per_engine():
    # a coarse "degraded" /health status must not blank out the per-engine probe
    health_degraded = resp(200, {"status": "degraded"})
    mega_probe = resp(200, MEGA_SEARCH_PAYLOAD)
    mock_get = AsyncMock(side_effect=[health_degraded, mega_probe])
    with patch("httpx.AsyncClient.get", mock_get):
        health = run(OpenSerpProvider().health())
    assert health.last_error == "degraded"
    assert len(health.engines) == 5


def test_openserp_health_unreachable():
    mock_get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient.get", mock_get):
        health = run(OpenSerpProvider().health())
    assert health.reachable is False
    assert health.engines == []
