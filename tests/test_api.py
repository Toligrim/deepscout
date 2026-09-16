import time
from unittest.mock import AsyncMock

from app import main as app_main
from app.services import aggregation, deep_search
from app.services.aggregation import AggregatedSearchResponse, MergedResult
from app.services.providers import SearchResponse


def _agg(results=None, backends=None, warnings=None):
    return AggregatedSearchResponse(
        query="q", results=results or [], backends=backends or {}, warnings=warnings or []
    )


def test_search_old_request_shape_still_works(client, monkeypatch):
    """A request with only the pre-v0.2 fields (no backends/openserp_engines) must still work."""

    async def fake_run_search(query, **kwargs):
        assert kwargs["backends"] is None or isinstance(kwargs["backends"], list)
        return _agg(
            results=[
                MergedResult(
                    canonical_url="https://example.com/a",
                    title="A",
                    snippet="snip",
                    backends=["searxng"],
                    engines=["brave"],
                    best_rank=1,
                    score=1.0,
                    contributions=[("searxng", "brave")],
                )
            ],
            backends={"searxng": {"status": "ok", "count": 1, "error": None}},
        )

    monkeypatch.setattr(app_main, "run_search", fake_run_search)

    resp = client.post("/api/search", json={"project_id": 1, "query": "raspberry pi"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "raspberry pi"
    assert data["count"] == 1
    assert data["results"][0]["canonical_url"] == "https://example.com/a"
    # new fields are additive, old UI code that ignores them keeps working
    assert "backends" in data
    assert "warnings" in data


def test_search_partial_backend_failure_returns_200(client, monkeypatch):
    async def fake_run_search(query, **kwargs):
        return _agg(
            results=[],
            backends={
                "searxng": {"status": "failed", "count": 0, "error": "connection refused"},
                "openserp": {"status": "ok", "count": 0, "error": None},
            },
            warnings=["searxng: connection refused"],
        )

    monkeypatch.setattr(app_main, "run_search", fake_run_search)

    resp = client.post("/api/search", json={"project_id": 1, "query": "q", "backends": ["searxng", "openserp"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["backends"]["searxng"]["status"] == "failed"
    assert data["warnings"]


def test_search_stores_provenance_for_every_contribution(client, monkeypatch):
    async def fake_run_search(query, **kwargs):
        return _agg(
            results=[
                MergedResult(
                    canonical_url="https://example.com/a",
                    title="A",
                    snippet="s",
                    backends=["searxng", "openserp"],
                    engines=["brave", "bing"],
                    best_rank=1,
                    score=1.9,
                    contributions=[("searxng", "brave"), ("openserp", "bing")],
                )
            ],
            backends={"searxng": {"status": "ok", "count": 1, "error": None}, "openserp": {"status": "ok", "count": 1, "error": None}},
        )

    monkeypatch.setattr(app_main, "run_search", fake_run_search)

    resp = client.post("/api/search", json={"project_id": 1, "query": "q", "backends": ["searxng", "openserp"]})
    assert resp.status_code == 200

    rows = client.get("/api/urls", params={"project_id": 1}).json()
    match = next(r for r in rows if r["url"] == "https://example.com/a")
    assert set(match["sources"].split(",")) == {"searxng", "openserp"}


def test_search_health_endpoint_shape(client, monkeypatch):
    async def fake_health(**kwargs):
        return {
            "searxng": {"reachable": True, "latency_ms": 5.0, "last_error": None, "engines": []},
            "openserp": {"reachable": False, "latency_ms": None, "last_error": "refused", "engines": []},
            "discovery": {"wayback": {"reachable": True}, "commoncrawl": {"reachable": True}},
        }

    monkeypatch.setattr(app_main, "get_search_health", fake_health)
    resp = client.get("/api/search/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["searxng"]["reachable"] is True
    assert data["openserp"]["reachable"] is False


def test_domain_explorer_unaffected_by_search_changes(client, monkeypatch):
    async def fake_sitemaps(domain, limit):
        return [{"url": "https://example.com/page", "source_detail": "https://example.com/sitemap.xml"}]

    monkeypatch.setattr(app_main, "discover_sitemaps", fake_sitemaps)

    resp = client.post(
        "/api/discover/domain",
        json={"project_id": 1, "domain": "example.com", "sources": ["sitemap"], "limit_per_source": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"]["sitemap"]["accepted"] == 1
    assert data["processed"] == 1


def test_search_never_calls_health_or_extra_probe_requests(client, monkeypatch):
    """The default (no explicit backends) Search request must go straight to both
    providers' search() in parallel — no health() precheck, no probe search first."""
    searxng_health = AsyncMock()
    openserp_health = AsyncMock()
    searxng_search = AsyncMock(return_value=SearchResponse(backend="searxng", status="ok", results=[]))
    openserp_search = AsyncMock(return_value=SearchResponse(backend="openserp", status="ok", results=[]))

    monkeypatch.setattr(aggregation.PROVIDERS["searxng"], "health", searxng_health)
    monkeypatch.setattr(aggregation.PROVIDERS["openserp"], "health", openserp_health)
    monkeypatch.setattr(aggregation.PROVIDERS["searxng"], "search", searxng_search)
    monkeypatch.setattr(aggregation.PROVIDERS["openserp"], "search", openserp_search)

    resp = client.post("/api/search", json={"project_id": 1, "query": "q"})
    assert resp.status_code == 200

    searxng_health.assert_not_awaited()
    openserp_health.assert_not_awaited()
    searxng_search.assert_awaited_once()
    openserp_search.assert_awaited_once()


def _wait_for_terminal(client, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/deep-search/{job_id}").json()
        if data["status"] not in {"queued", "running"}:
            return data
        time.sleep(0.05)
    raise AssertionError("job did not reach a terminal status in time")


def test_deep_search_create_and_poll_to_completion(client, monkeypatch):
    async def fake_run_search(query, **kwargs):
        return AggregatedSearchResponse(query=query, results=[], backends={}, warnings=[])

    monkeypatch.setattr(deep_search, "run_search", fake_run_search)
    for source in ("sitemap", "wayback", "commoncrawl"):
        monkeypatch.setitem(deep_search._SOURCE_CALLS, source, AsyncMock(return_value=[]))

    resp = client.post("/api/deep-search", json={"project_id": 1, "query": "bgp routing"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    final = _wait_for_terminal(client, job_id)
    assert final["status"] == "failed"  # zero SERP results, zero discovery -> nothing usable
    assert final["result_summary"]["counters"]["unique_urls"] == 0


def test_deep_search_rejects_invalid_sources(client):
    resp = client.post("/api/deep-search", json={"project_id": 1, "query": "q", "sources": ["live_crawl"]})
    assert resp.status_code == 400


def test_deep_search_rejects_a_typo_source_even_alongside_valid_ones(client):
    # a typo like "waybak" must not be silently dropped just because "wayback" is
    # also present -- strict validation, not "ignore anything unrecognized"
    resp = client.post("/api/deep-search", json={"project_id": 1, "query": "q", "sources": ["wayback", "waybak"]})
    assert resp.status_code == 400
    assert "waybak" in resp.json()["detail"]


def test_deep_search_job_not_found_returns_404(client):
    assert client.get("/api/deep-search/999999").status_code == 404
    assert client.get("/api/deep-search/999999/results").status_code == 404


def test_deep_search_results_endpoint_filters_by_tier(client, monkeypatch):
    async def fake_run_search(query, **kwargs):
        return AggregatedSearchResponse(
            query=query,
            results=[
                MergedResult(
                    canonical_url="https://example.com/bgp", title="BGP", snippet="s",
                    backends=["searxng"], engines=["brave"], best_rank=1, score=1.0,
                    contributions=[("searxng", "brave")],
                )
            ],
            backends={"searxng": {"status": "ok", "count": 1, "error": None}}, warnings=[],
        )

    monkeypatch.setattr(deep_search, "run_search", fake_run_search)
    for source in ("sitemap", "wayback", "commoncrawl"):
        monkeypatch.setitem(deep_search._SOURCE_CALLS, source, AsyncMock(return_value=[]))

    job_id = client.post("/api/deep-search", json={"project_id": 1, "query": "bgp"}).json()["job_id"]
    _wait_for_terminal(client, job_id)

    high = client.get(f"/api/deep-search/{job_id}/results").json()
    assert len(high) == 1
    assert high[0]["url"] == "https://example.com/bgp"

    everything = client.get(f"/api/deep-search/{job_id}/results", params={"tiers": "all"}).json()
    assert len(everything) == 1


def test_deep_search_results_uses_the_jobs_own_project_not_a_client_supplied_one(client, monkeypatch):
    other_project = client.post("/api/projects", json={"name": "Other project"}).json()

    async def fake_run_search(query, **kwargs):
        return AggregatedSearchResponse(
            query=query,
            results=[
                MergedResult(
                    canonical_url="https://example.com/bgp", title="BGP", snippet="s",
                    backends=["searxng"], engines=["brave"], best_rank=1, score=1.0,
                    contributions=[("searxng", "brave")],
                )
            ],
            backends={"searxng": {"status": "ok", "count": 1, "error": None}}, warnings=[],
        )

    monkeypatch.setattr(deep_search, "run_search", fake_run_search)
    for source in ("sitemap", "wayback", "commoncrawl"):
        monkeypatch.setitem(deep_search._SOURCE_CALLS, source, AsyncMock(return_value=[]))

    job_id = client.post("/api/deep-search", json={"project_id": other_project["id"], "query": "bgp"}).json()["job_id"]
    _wait_for_terminal(client, job_id)

    # no project_id in the request at all -- the endpoint has no such parameter anymore,
    # it must resolve the project from the job itself and still return the right rows
    results = client.get(f"/api/deep-search/{job_id}/results").json()
    assert len(results) == 1
    assert results[0]["url"] == "https://example.com/bgp"
    assert client.get(f"/api/deep-search/{job_id}").json()["project_id"] == other_project["id"]


def test_deep_search_events_stream_reports_terminal_status(client, monkeypatch):
    async def fake_run_search(query, **kwargs):
        return AggregatedSearchResponse(query=query, results=[], backends={}, warnings=[])

    monkeypatch.setattr(deep_search, "run_search", fake_run_search)
    for source in ("sitemap", "wayback", "commoncrawl"):
        monkeypatch.setitem(deep_search._SOURCE_CALLS, source, AsyncMock(return_value=[]))

    job_id = client.post("/api/deep-search", json={"project_id": 1, "query": "q"}).json()["job_id"]
    _wait_for_terminal(client, job_id)

    with client.stream("GET", f"/api/deep-search/{job_id}/events") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: done" in body
