from __future__ import annotations

import asyncio
import time

import httpx

from ..config import settings
from .aggregation import PROVIDERS
from .providers import BackendHealth

_cache: dict | None = None
_cache_at: float = 0.0
_lock = asyncio.Lock()


def _health_to_dict(h: BackendHealth) -> dict:
    return {
        "reachable": h.reachable,
        "latency_ms": round(h.latency_ms, 1) if h.latency_ms is not None else None,
        "last_error": h.last_error,
        "engines": [{"name": e.name, "status": e.status, "reason": e.reason} for e in h.engines],
    }


async def _discovery_ping(name: str, url: str) -> dict:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": settings.user_agent}) as client:
            resp = await client.get(url)
        return {
            "reachable": resp.status_code < 500,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "last_error": None if resp.status_code < 500 else f"HTTP {resp.status_code}",
        }
    except httpx.HTTPError as exc:
        return {"reachable": False, "latency_ms": None, "last_error": str(exc)}


async def _compute() -> dict:
    async def safe_health(name: str) -> BackendHealth:
        try:
            return await PROVIDERS[name].health()
        except Exception as exc:  # a bug in one provider's health() must not break the endpoint
            return BackendHealth(backend=name, reachable=False, latency_ms=None, engines=[], last_error=str(exc))

    searxng_health, openserp_health, wayback, commoncrawl = await asyncio.gather(
        safe_health("searxng"),
        safe_health("openserp"),
        _discovery_ping("wayback", "https://web.archive.org/cdx/search/cdx?url=example.com&limit=1&output=json"),
        _discovery_ping("commoncrawl", "https://index.commoncrawl.org/collinfo.json"),
    )
    return {
        "searxng": _health_to_dict(searxng_health),
        "openserp": _health_to_dict(openserp_health),
        "discovery": {"wayback": wayback, "commoncrawl": commoncrawl},
        "generated_at": time.time(),
    }


async def get_search_health(*, force: bool = False) -> dict:
    global _cache, _cache_at
    async with _lock:
        now = time.monotonic()
        if not force and _cache is not None and (now - _cache_at) < settings.search_health_cache_seconds:
            return _cache
        _cache = await _compute()
        _cache_at = now
        return _cache
