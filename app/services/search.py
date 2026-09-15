from __future__ import annotations

import time

import httpx

from ..config import settings
from .providers import BackendHealth, EngineHealth, SearchResponse, SearchResult

# SearXNG's unresponsive_engines reasons are free-text strings written by each engine
# module (see searx/search/processors). We only ever saw "CAPTCHA" and "access denied"
# on this instance, but map a few other common ones defensively; anything unmatched
# keeps its original (truncated) text instead of being silently dropped.
_REASON_KEYWORDS = (
    ("captcha", "CAPTCHA"),
    ("denied", "blocked"),
    ("blocked", "blocked"),
    ("403", "blocked"),
    ("429", "rate_limited"),
    ("too many", "rate_limited"),
    ("timeout", "timeout"),
    ("timed out", "timeout"),
)


def _classify_reason(raw: str) -> str:
    low = raw.lower()
    for needle, label in _REASON_KEYWORDS:
        if needle in low:
            return label
    return raw[:60]


async def _get_json(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    response = await client.get(f"{settings.searxng_url}{path}", params=params)
    response.raise_for_status()
    return response.json()


class SearxngProvider:
    name = "searxng"

    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str = "all",
        time_range: str | None = None,
        engines: list[str] | None = None,
    ) -> SearchResponse:
        params: dict[str, str | int] = {
            "q": query,
            "format": "json",
            "pageno": max(page, 1),
            "language": language or "all",
            "safesearch": 0,
        }
        if time_range in {"day", "week", "month", "year"}:
            params["time_range"] = time_range
        if engines:
            params["engines"] = ",".join(engines)

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            ) as client:
                payload = await _get_json(client, "/search", params)
        except httpx.HTTPError as exc:
            return SearchResponse(backend=self.name, status="failed", results=[], error=str(exc))
        latency_ms = (time.monotonic() - started) * 1000

        results: list[SearchResult] = []
        for rank, item in enumerate(payload.get("results", []), start=1):
            url = item.get("url")
            if not url:
                continue
            engines_hit = item.get("engines") or ([item.get("engine")] if item.get("engine") else [])
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title"),
                    snippet=item.get("content") or item.get("snippet"),
                    backend=self.name,
                    engines=[e for e in engines_hit if e],
                    rank=rank,
                    score=item.get("score"),
                    published_at=item.get("publishedDate"),
                    metadata={"positions": item.get("positions")},
                )
            )

        # unresponsive_engines is present on every /search response, not just the
        # dedicated health probe — surface it here too instead of only in health().
        warnings = [
            f"{name}: {_classify_reason(reason)}" for name, reason in payload.get("unresponsive_engines", [])
        ]
        status = "degraded" if warnings else "ok"
        return SearchResponse(
            backend=self.name, status=status, results=results, latency_ms=latency_ms, warnings=warnings
        )

    async def health(self) -> BackendHealth:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout,
                headers={"User-Agent": settings.user_agent},
                follow_redirects=True,
            ) as client:
                config_payload = await _get_json(client, "/config")
                probe_payload = await _get_json(
                    client,
                    "/search",
                    {"q": "test", "format": "json", "safesearch": 0},
                )
        except httpx.HTTPError as exc:
            return BackendHealth(
                backend=self.name, reachable=False, latency_ms=None, engines=[], last_error=str(exc)
            )
        latency_ms = (time.monotonic() - started) * 1000

        enabled = {e["name"] for e in config_payload.get("engines", []) if e.get("enabled")}
        responded = set()
        for item in probe_payload.get("results", []):
            for e in item.get("engines") or ([item.get("engine")] if item.get("engine") else []):
                if e:
                    responded.add(e)
        unresponsive = {name: reason for name, reason in probe_payload.get("unresponsive_engines", [])}

        engines: list[EngineHealth] = []
        for name in sorted(enabled):
            if name in unresponsive:
                engines.append(EngineHealth(name=name, status="degraded", reason=_classify_reason(unresponsive[name])))
            elif name in responded:
                engines.append(EngineHealth(name=name, status="ok"))
            else:
                engines.append(EngineHealth(name=name, status="unknown"))

        return BackendHealth(backend=self.name, reachable=True, latency_ms=latency_ms, engines=engines)
