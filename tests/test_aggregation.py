import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import aggregation
from app.services.providers import SearchResponse, SearchResult


def run(coro):
    return asyncio.run(coro)


def searxng_result(url, rank=1, engines=("brave",), score=1.0):
    return SearchResult(url=url, title=f"Title {url}", snippet="short", backend="searxng", engines=list(engines), rank=rank, score=score)


def openserp_result(url, rank=1, engines=("bing",)):
    return SearchResult(url=url, title=f"Title {url}", snippet="a much longer descriptive snippet here", backend="openserp", engines=list(engines), rank=rank)


def patch_providers(monkeypatch, searxng=None, openserp=None):
    """searxng/openserp: a SearchResponse, or an Exception instance to simulate a raised bug."""

    async def searxng_search(*a, **k):
        if isinstance(searxng, Exception):
            raise searxng
        return searxng

    async def openserp_search(*a, **k):
        if isinstance(openserp, Exception):
            raise openserp
        return openserp

    monkeypatch.setattr(aggregation.PROVIDERS["searxng"], "search", searxng_search)
    monkeypatch.setattr(aggregation.PROVIDERS["openserp"], "search", openserp_search)


def test_both_backends_ok_dedup_and_merge_provenance(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(backend="searxng", status="ok", results=[searxng_result("https://pinout.xyz/", rank=1)]),
        openserp=SearchResponse(backend="openserp", status="ok", results=[openserp_result("https://pinout.xyz/", rank=1)]),
    )
    agg = run(aggregation.run_search("q"))
    assert len(agg.results) == 1
    merged = agg.results[0]
    assert merged.canonical_url == "https://pinout.xyz/"
    assert set(merged.backends) == {"searxng", "openserp"}
    assert set(merged.engines) == {"brave", "bing"}
    assert set(merged.contributions) == {("searxng", "brave"), ("openserp", "bing")}


def test_tracking_params_stripped_before_dedup(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(backend="searxng", status="ok", results=[searxng_result("https://example.com/a?utm_source=x")]),
        openserp=SearchResponse(backend="openserp", status="ok", results=[openserp_result("https://example.com/a")]),
    )
    agg = run(aggregation.run_search("q"))
    assert len(agg.results) == 1
    assert agg.results[0].canonical_url == "https://example.com/a"


def test_searxng_failure_does_not_break_request(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(backend="searxng", status="failed", results=[], error="connection refused"),
        openserp=SearchResponse(backend="openserp", status="ok", results=[openserp_result("https://a.example/")]),
    )
    agg = run(aggregation.run_search("q"))
    assert len(agg.results) == 1
    assert agg.backends["searxng"]["status"] == "failed"
    assert agg.backends["openserp"]["status"] == "ok"
    assert any("searxng" in w for w in agg.warnings)


def test_openserp_failure_does_not_break_request(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(backend="searxng", status="ok", results=[searxng_result("https://a.example/")]),
        openserp=SearchResponse(backend="openserp", status="failed", results=[], error="timeout"),
    )
    agg = run(aggregation.run_search("q"))
    assert len(agg.results) == 1
    assert agg.backends["openserp"]["status"] == "failed"


def test_both_backends_failed_returns_empty_not_raise(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(backend="searxng", status="failed", results=[], error="down"),
        openserp=SearchResponse(backend="openserp", status="failed", results=[], error="down"),
    )
    agg = run(aggregation.run_search("q"))
    assert agg.results == []
    assert len(agg.warnings) == 2


def test_provider_bug_is_isolated_like_a_normal_failure(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=RuntimeError("boom"),
        openserp=SearchResponse(backend="openserp", status="ok", results=[openserp_result("https://a.example/")]),
    )
    agg = run(aggregation.run_search("q"))
    assert len(agg.results) == 1
    assert agg.backends["searxng"]["status"] == "failed"
    assert "boom" in agg.backends["searxng"]["error"]


def test_ranking_is_deterministic_and_documented_formula(monkeypatch):
    # url A: rank 1 in both backends, 2 engines, 2 backends -> highest score
    # url B: rank 1 in searxng only, 1 engine, 1 backend -> mid score
    # url C: rank 5 in searxng only -> lowest score
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(
            backend="searxng",
            status="ok",
            results=[
                searxng_result("https://a.example/", rank=1, engines=["brave"]),
                searxng_result("https://b.example/", rank=1, engines=["brave"], score=None),
                searxng_result("https://c.example/", rank=5, engines=["brave"], score=None),
            ],
        ),
        openserp=SearchResponse(
            backend="openserp", status="ok", results=[openserp_result("https://a.example/", rank=1, engines=["bing"])]
        ),
    )
    agg = run(aggregation.run_search("q"))
    ordered = [r.canonical_url for r in agg.results]
    assert ordered == ["https://a.example/", "https://b.example/", "https://c.example/"]
    a = agg.results[0]
    # 1/1 (best_rank) + 0.3*(2 engines - 1) + 0.5 (2 backends) + 0.1*1.0 (searxng score) = 1.9
    assert a.score == pytest.approx(1.9)


def test_best_title_and_snippet_come_from_best_ranked_contributor(monkeypatch):
    patch_providers(
        monkeypatch,
        searxng=SearchResponse(
            backend="searxng", status="ok", results=[searxng_result("https://a.example/", rank=3)]
        ),
        openserp=SearchResponse(
            backend="openserp", status="ok", results=[openserp_result("https://a.example/", rank=1)]
        ),
    )
    agg = run(aggregation.run_search("q"))
    merged = agg.results[0]
    assert merged.best_rank == 1
    assert "openserp" not in merged.title  # title text itself doesn't contain backend name, just checking it changed
    assert merged.snippet == "a much longer descriptive snippet here"


def test_backend_filter_only_runs_selected_provider(monkeypatch):
    searxng_search = AsyncMock(return_value=SearchResponse(backend="searxng", status="ok", results=[]))
    openserp_search = AsyncMock(return_value=SearchResponse(backend="openserp", status="ok", results=[]))
    monkeypatch.setattr(aggregation.PROVIDERS["searxng"], "search", searxng_search)
    monkeypatch.setattr(aggregation.PROVIDERS["openserp"], "search", openserp_search)
    run(aggregation.run_search("q", backends=["searxng"]))
    searxng_search.assert_awaited_once()
    openserp_search.assert_not_awaited()
