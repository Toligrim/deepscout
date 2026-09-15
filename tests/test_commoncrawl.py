import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.discovery import COMMONCRAWL_MAX_RETRIES, discover_commoncrawl

COLLINFO = [
    {"id": "CC-MAIN-2026-34", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-34-index"},
    {"id": "CC-MAIN-2026-30", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-30-index"},
    {"id": "CC-MAIN-2026-25", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-25-index"},
    {"id": "CC-MAIN-2026-21", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-21-index"},
    {"id": "CC-MAIN-2026-17", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-17-index"},
]


def cdx_row(url: str, **extra) -> str:
    row = {"url": url, "timestamp": "20260101000000", "status": "200", "digest": "abc"}
    row.update(extra)
    return json.dumps(row)


def make_response(status_code: int, text: str = "") -> httpx.Response:
    return httpx.Response(status_code, text=text, request=httpx.Request("GET", "https://index.commoncrawl.org/x"))


def collinfo_response() -> httpx.Response:
    return make_response(200, text=json.dumps(COLLINFO))


def empty_ok_response() -> httpx.Response:
    return make_response(200, text="")


def run(coro):
    return asyncio.run(coro)


async def _noop_sleep(_delay):
    return None


def test_first_collection_502_second_works():
    # collection 1 exhausts its retries on 502, collection 2 succeeds, 3-5 have no matches
    responses = (
        [collinfo_response()]
        + [make_response(502)] * (COMMONCRAWL_MAX_RETRIES + 1)
        + [make_response(200, text=cdx_row("https://example.com/a"))]
        + [empty_ok_response()] * 3
    )
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        results = run(discover_commoncrawl("example.com", limit=100, sleep=_noop_sleep))
    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/a"
    assert results[0]["collection"] == "CC-MAIN-2026-30"
    assert mock_get.await_count == len(responses)


def test_429_then_retry_success_same_collection():
    # collection 1: first attempt 429, retry succeeds -> stays on collection 1
    responses = (
        [collinfo_response(), make_response(429), make_response(200, text=cdx_row("https://example.com/a"))]
        + [empty_ok_response()] * 4
    )
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        results = run(discover_commoncrawl("example.com", limit=100, sleep=_noop_sleep))
    assert len(results) == 1
    assert results[0]["collection"] == "CC-MAIN-2026-34"


def test_400_moves_to_next_collection_without_retry():
    responses = (
        [collinfo_response(), make_response(400, text="bad request"), make_response(200, text=cdx_row("https://example.com/a"))]
        + [empty_ok_response()] * 3
    )
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        results = run(discover_commoncrawl("example.com", limit=100, sleep=_noop_sleep))
    assert len(results) == 1
    assert results[0]["collection"] == "CC-MAIN-2026-30"
    # exactly one attempt against the 400 collection: no retry loop for 400
    assert mock_get.await_count == len(responses)


def test_duplicate_url_across_collections_deduplicated():
    responses = [
        collinfo_response(),
        make_response(200, text=cdx_row("https://example.com/a")),
        make_response(200, text=cdx_row("https://example.com/a") + "\n" + cdx_row("https://example.com/b")),
        empty_ok_response(),
        empty_ok_response(),
        empty_ok_response(),
    ]
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        results = run(discover_commoncrawl("example.com", limit=100, sleep=_noop_sleep))
    urls = sorted(r["url"] for r in results)
    assert urls == ["https://example.com/a", "https://example.com/b"]
    # "a" appears in both collection 1 and 2 but must be stored once, from the first hit
    a = next(r for r in results if r["url"] == "https://example.com/a")
    assert a["collection"] == "CC-MAIN-2026-34"


def test_all_collections_unavailable_raises_but_other_providers_unaffected():
    # every collection returns 503 forever; bounded retries still terminate
    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=_infinite_503(COLLINFO))):
        with pytest.raises(RuntimeError):
            run(discover_commoncrawl("example.com", limit=100, sleep=_noop_sleep))
    # discover_commoncrawl raising is exactly what lets app.main's per-provider
    # try/except isolate the failure without aborting sitemap/wayback providers


def _infinite_503(collinfo):
    first = True

    async def _gen(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            return collinfo_response()
        return make_response(503)

    return _gen


def test_limit_is_respected_across_collections():
    many_rows = "\n".join(cdx_row(f"https://example.com/{i}") for i in range(10))
    responses = [collinfo_response(), make_response(200, text=many_rows)]
    mock_get = AsyncMock(side_effect=responses)
    with patch("httpx.AsyncClient.get", mock_get):
        results = run(discover_commoncrawl("example.com", limit=5, sleep=_noop_sleep))
    assert len(results) == 5
    # limit reached after collection 1 -> collections 2-5 must not even be queried
    assert mock_get.await_count == 2
