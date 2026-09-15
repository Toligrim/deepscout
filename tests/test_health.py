import asyncio
import dataclasses
import ssl
from unittest.mock import AsyncMock, patch

from app.config import settings as app_settings
from app.services import health
from app.services.providers import BackendHealth


def run(coro):
    return asyncio.run(coro)


def _ok(backend):
    return BackendHealth(backend=backend, reachable=True, latency_ms=1.0, engines=[])


def test_health_result_is_cached_within_ttl(monkeypatch):
    health._cache = None
    health._cache_at = 0.0
    monkeypatch.setattr(health, "settings", dataclasses.replace(app_settings, search_health_cache_seconds=60))

    calls = {"n": 0}

    async def fake_health():
        calls["n"] += 1
        return _ok("searxng")

    monkeypatch.setattr(health.PROVIDERS["searxng"], "health", fake_health)
    monkeypatch.setattr(health.PROVIDERS["openserp"], "health", fake_health)
    monkeypatch.setattr(health, "_discovery_ping", lambda name, url: asyncio.sleep(0, result={"reachable": True}))

    run(health.get_search_health())
    run(health.get_search_health())
    assert calls["n"] == 2  # searxng + openserp, once each — second call served from cache


def test_health_force_refresh_bypasses_cache(monkeypatch):
    health._cache = None
    health._cache_at = 0.0
    monkeypatch.setattr(health, "settings", dataclasses.replace(app_settings, search_health_cache_seconds=60))

    calls = {"n": 0}

    async def fake_health():
        calls["n"] += 1
        return _ok("searxng")

    monkeypatch.setattr(health.PROVIDERS["searxng"], "health", fake_health)
    monkeypatch.setattr(health.PROVIDERS["openserp"], "health", fake_health)
    monkeypatch.setattr(health, "_discovery_ping", lambda name, url: asyncio.sleep(0, result={"reachable": True}))

    run(health.get_search_health())
    run(health.get_search_health(force=True))
    assert calls["n"] == 4


def test_health_provider_bug_does_not_crash_endpoint(monkeypatch):
    health._cache = None
    health._cache_at = 0.0

    async def boom():
        raise RuntimeError("bug")

    monkeypatch.setattr(health.PROVIDERS["searxng"], "health", boom)
    monkeypatch.setattr(health.PROVIDERS["openserp"], "health", lambda: asyncio.sleep(0, result=_ok("openserp")))
    monkeypatch.setattr(health, "_discovery_ping", lambda name, url: asyncio.sleep(0, result={"reachable": True}))

    result = run(health.get_search_health(force=True))
    assert result["searxng"]["reachable"] is False
    assert "bug" in result["searxng"]["last_error"]
    assert result["openserp"]["reachable"] is True


def test_discovery_ping_survives_non_httpx_transport_error():
    # Regression: a bare ssl.SSLError (observed in production against web.archive.org)
    # is not a subclass of httpx.HTTPError and previously escaped uncaught, crashing
    # the whole /api/search/health endpoint via asyncio.gather.
    mock_get = AsyncMock(side_effect=ssl.SSLError("decryption failed or bad record mac"))
    with patch("httpx.AsyncClient.get", mock_get):
        result = run(health._discovery_ping("wayback", "https://web.archive.org/cdx/search/cdx"))
    assert result["reachable"] is False
    assert "decryption failed" in result["last_error"]


def test_health_endpoint_survives_discovery_transport_error(monkeypatch):
    health._cache = None
    health._cache_at = 0.0

    monkeypatch.setattr(health.PROVIDERS["searxng"], "health", lambda: asyncio.sleep(0, result=_ok("searxng")))
    monkeypatch.setattr(health.PROVIDERS["openserp"], "health", lambda: asyncio.sleep(0, result=_ok("openserp")))

    mock_get = AsyncMock(side_effect=ssl.SSLError("decryption failed or bad record mac"))
    with patch("httpx.AsyncClient.get", mock_get):
        result = run(health.get_search_health(force=True))

    assert result["discovery"]["wayback"]["reachable"] is False
    assert result["discovery"]["commoncrawl"]["reachable"] is False
    assert result["searxng"]["reachable"] is True
