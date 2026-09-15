import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.openserp import OpenSerpProvider, _date_param
from app.services.search import SearxngProvider


def run(coro):
    return asyncio.run(coro)


def resp(status_code: int, payload) -> httpx.Response:
    return httpx.Response(
        status_code, json=payload, request=httpx.Request("GET", "https://x/")
    )


def last_call_params(mock_get) -> dict:
    return mock_get.await_args.kwargs.get("params") or {}


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

SEARXNG_SEARCH_PAYLOAD_CLEAN = {**SEARXNG_SEARCH_PAYLOAD, "unresponsive_engines": []}

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
    mock_get = AsyncMock(return_value=resp(200, SEARXNG_SEARCH_PAYLOAD_CLEAN))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(SearxngProvider().search("raspberry pi gpio"))
    assert response.status == "ok"
    assert [r.url for r in response.results] == ["https://pinout.xyz/", "https://example.com/gpio"]
    assert response.results[0].rank == 1
    assert response.results[0].engines == ["brave"]
    assert response.results[1].rank == 2


def test_searxng_search_surfaces_unresponsive_engines_as_degraded():
    mock_get = AsyncMock(return_value=resp(200, SEARXNG_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(SearxngProvider().search("raspberry pi gpio"))
    assert response.status == "degraded"
    assert len(response.results) == 2  # partial results still come through
    assert sorted(response.warnings) == ["duckduckgo: CAPTCHA", "mojeek: blocked", "startpage: CAPTCHA"]


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
#
# Fixture mirrors a real /mega/search?dedupe=false&merge=true response captured against
# a live OpenSERP v0.8.12 instance: https://www.python.org/ found by both duckduckgo
# (rank 1) and bing (rank 1); one bing ad; one duckduckgo-only result.

MEGA_SEARCH_PAYLOAD = {
    "query": {"text": "python", "engines_requested": ["bing", "duckduckgo"]},
    "meta": {
        "engines_responded": ["duckduckgo", "bing"],
        "engines_failed": [],
        "engine_errors": [],
    },
    "results": [
        {
            "id": "s_ad1",
            "type": "ad",
            "title": "Sponsored",
            "url": "https://www.bing.com/aclick?ld=xyz",
            "engine": "bing",
        },
        {
            "id": "s_dd1",
            "type": "organic",
            "title": "Welcome to Python.org",
            "snippet": "Python is a versatile and easy-to-learn language.",
            "url": "https://www.python.org/",
            "domain": "python.org",
            "engine": "duckduckgo",
            "rank": 1,
        },
        {
            "id": "s_bi1",
            "type": "organic",
            "title": "Welcome to Python.org",
            "snippet": "Experienced programmers in any other language can pick up Python quickly.",
            "url": "https://www.python.org/",
            "domain": "python.org",
            "engine": "bing",
            "rank": 1,
        },
        {
            "id": "s_dd2",
            "type": "organic",
            "title": "Python (programming language) - Wikipedia",
            "snippet": "Python is a high-level language.",
            "url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "domain": "en.wikipedia.org",
            "engine": "duckduckgo",
            "rank": 2,
        },
    ],
    "clusters": [
        {
            "id": "c_ad1",
            "canonical_url": "https://www.bing.com/aclick?ld=xyz",
            "domain": "bing.com",
            "title": "Sponsored",
            "occurrences": [{"engine": "bing", "rank": 1, "result_id": "s_ad1"}],
            "engines_count": 1,
            "best_rank": 1,
            "score": 0.5,
        },
        {
            "id": "c_py",
            "canonical_url": "https://www.python.org/",
            "domain": "python.org",
            "title": "Welcome to Python.org",
            "occurrences": [
                {"engine": "duckduckgo", "rank": 1, "result_id": "s_dd1"},
                {"engine": "bing", "rank": 1, "result_id": "s_bi1"},
            ],
            "engines_count": 2,
            "best_rank": 1,
            "score": 1.0,
        },
        {
            "id": "c_wiki",
            "canonical_url": "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "domain": "en.wikipedia.org",
            "title": "Python (programming language) - Wikipedia",
            "occurrences": [{"engine": "duckduckgo", "rank": 2, "result_id": "s_dd2"}],
            "engines_count": 1,
            "best_rank": 2,
            "score": 0.5,
        },
    ],
}

MEGA_SEARCH_WITH_ERRORS_PAYLOAD = {
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
            "type": "organic",
            "title": "Raspberry Pi GPIO Pinout",
            "snippet": "Learn about the GPIO pins.",
            "url": "https://pinout.xyz/",
            "domain": "pinout.xyz",
            "engine": "bing",
            "rank": 1,
        },
    ],
    "clusters": [
        {
            "id": "c_1",
            "canonical_url": "https://pinout.xyz/",
            "domain": "pinout.xyz",
            "title": "Raspberry Pi GPIO Pinout",
            "occurrences": [{"engine": "bing", "rank": 1, "result_id": "s_1"}],
            "engines_count": 1,
            "best_rank": 1,
            "score": 1.0,
        }
    ],
}


def test_openserp_search_merges_multi_engine_cluster_provenance():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("python"))
    assert response.status == "ok"
    by_url = {r.url: r for r in response.results}
    assert "https://www.bing.com/aclick?ld=xyz" not in by_url  # ad cluster skipped
    python_result = by_url["https://www.python.org/"]
    assert set(python_result.engines) == {"duckduckgo", "bing"}
    assert python_result.rank == 1
    assert python_result.snippet  # resolved via the best (lowest-rank) occurrence
    assert by_url["https://en.wikipedia.org/wiki/Python_(programming_language)"].engines == ["duckduckgo"]


def test_openserp_search_degraded_status_with_warnings():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_WITH_ERRORS_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("raspberry pi gpio"))
    assert response.status == "degraded"
    assert len(response.results) == 1
    assert sorted(response.warnings) == ["ecosia: blocked", "google: CAPTCHA", "yandex: CAPTCHA"]


def test_openserp_search_all_engines_failed_is_failed_status():
    error_body = {
        "error": "all_engines_failed",
        "code": 502,
        "message": "all selected engines failed",
    }
    mock_get = AsyncMock(return_value=resp(502, error_body))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("q"))
    assert response.status == "failed"
    assert response.results == []
    assert "all selected engines failed" in response.error


def test_openserp_search_http_error_is_isolated():
    mock_get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    with patch("httpx.AsyncClient.get", mock_get):
        response = run(OpenSerpProvider().search("q"))
    assert response.status == "failed"
    assert response.results == []


def test_openserp_search_sends_real_query_params():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        run(OpenSerpProvider().search("python", language="ru", time_range="week", page=1))
    params = last_call_params(mock_get)
    assert params["lang"] == "ru"
    assert params["date"] == _date_param("week")
    assert params["start"] == 0
    assert params["limit"] == 20
    assert params["dedupe"] == "false"


def test_openserp_search_language_all_is_not_sent():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        run(OpenSerpProvider().search("python", language="all", time_range=None))
    params = last_call_params(mock_get)
    assert "lang" not in params
    assert "date" not in params


def test_openserp_search_page_maps_to_start_offset():
    mock_get = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get):
        run(OpenSerpProvider().search("python", page=2))
    assert last_call_params(mock_get)["start"] == 20  # (page - 1) * DEFAULT_LIMIT

    mock_get2 = AsyncMock(return_value=resp(200, MEGA_SEARCH_PAYLOAD))
    with patch("httpx.AsyncClient.get", mock_get2):
        run(OpenSerpProvider().search("python", page=1))
    assert last_call_params(mock_get2)["start"] == 0


@pytest.mark.parametrize(
    "time_range,expected_days",
    [("day", 1), ("week", 7), ("month", 30), ("year", 365)],
)
def test_date_param_formats_yyyymmdd_range(time_range, expected_days):
    import datetime

    today = datetime.date(2026, 9, 15)
    value = _date_param(time_range, today=today)
    start = today - datetime.timedelta(days=expected_days)
    assert value == f"{start:%Y%m%d}..{today:%Y%m%d}"


def test_date_param_none_for_unknown_or_missing_range():
    assert _date_param(None) is None
    assert _date_param("") is None
    assert _date_param("decade") is None


def test_openserp_health_probes_real_engine_state():
    health_ok = resp(200, {"status": "healthy"})
    mega_probe = resp(200, MEGA_SEARCH_WITH_ERRORS_PAYLOAD)
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
    mega_probe = resp(200, MEGA_SEARCH_WITH_ERRORS_PAYLOAD)
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
