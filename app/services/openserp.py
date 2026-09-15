from __future__ import annotations

import time

import httpx

from ..config import settings
from .providers import BackendHealth, EngineHealth, SearchResponse, SearchResult

# Baidu is opt-in only (not part of the RU/EN workflow) and excluded from the default set.
# Whichever engines are queried, DeepScout never tries to defeat a CAPTCHA/block or otherwise
# evade anti-bot protection — an engine failing that way is just reported as degraded.
DEFAULT_ENGINES = ["google", "bing", "yandex", "duckduckgo", "ecosia"]
ALL_ENGINES = ["google", "bing", "yandex", "duckduckgo", "ecosia", "baidu"]

# OpenSERP v0.8.12 error codes (see /mega/search meta.engine_errors[].error), mapped to
# DeepScout's own short health-reason vocabulary.
_ERROR_LABELS = {
    "captcha_detected": "CAPTCHA",
    "blocked": "blocked",
    "rate_limited": "rate_limited",
    "proxy_timeout": "timeout",
    "timeout": "timeout",
    "empty_result": "empty_result",
    "engine_internal": "error",
    "circuit_open": "unavailable",
}


def _label(error_code: str) -> str:
    return _ERROR_LABELS.get(error_code, error_code[:40] or "error")


class OpenSerpProvider:
    name = "openserp"

    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str = "all",
        time_range: str | None = None,
        engines: list[str] | None = None,
    ) -> SearchResponse:
        engines = engines or DEFAULT_ENGINES
        params = {"text": query, "engines": ",".join(engines)}

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.openserp_timeout,
                headers={"User-Agent": settings.user_agent},
            ) as client:
                response = await client.get(f"{settings.openserp_url}/mega/search", params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            return SearchResponse(backend=self.name, status="failed", results=[], error=str(exc))
        latency_ms = (time.monotonic() - started) * 1000

        results: list[SearchResult] = []
        for item in payload.get("results", []):
            if item.get("type") == "ad":
                continue
            url = item.get("url")
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title"),
                    snippet=item.get("snippet"),
                    backend=self.name,
                    engines=[item["engine"]] if item.get("engine") else [],
                    rank=item.get("rank") or (item.get("position") or {}).get("absolute") or 999,
                    metadata={"domain": item.get("domain")},
                )
            )

        warnings = [
            f"{err.get('engine')}: {_label(err.get('error', ''))}"
            for err in (payload.get("meta") or {}).get("engine_errors", [])
        ]
        return SearchResponse(
            backend=self.name, status="ok", results=results, latency_ms=latency_ms, warnings=warnings
        )

    async def health(self) -> BackendHealth:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.timeout, headers={"User-Agent": settings.user_agent}
            ) as client:
                health_resp = await client.get(f"{settings.openserp_url}/health")
                health_resp.raise_for_status()
                health_payload = health_resp.json()
        except httpx.HTTPError as exc:
            return BackendHealth(
                backend=self.name, reachable=False, latency_ms=None, engines=[], last_error=str(exc)
            )
        # The coarse top-level status ("healthy"/"degraded"/...) only summarizes whether
        # *some* engine is unhappy; it is not a reason to skip the per-engine probe below
        # — a single stuck engine must not blank out every other engine's real status.
        overall_note = None if health_payload.get("status") == "healthy" else str(health_payload.get("status"))

        # /health only proves the process is up, not that any engine can actually reach
        # a search engine right now (circuit breakers stay "closed" until ~5 consecutive
        # failures, so they lag reality). A cheap real /mega/search probe — same idea as
        # SearxngProvider.health()'s probe query — is the only honest signal, and it's
        # cached by the same TTL as everything else in health.py.
        try:
            async with httpx.AsyncClient(
                timeout=settings.openserp_timeout, headers={"User-Agent": settings.user_agent}
            ) as client:
                probe = await client.get(
                    f"{settings.openserp_url}/mega/search",
                    params={"text": "test", "engines": ",".join(DEFAULT_ENGINES)},
                )
                probe.raise_for_status()
                probe_payload = probe.json()
        except httpx.HTTPError as exc:
            return BackendHealth(
                backend=self.name,
                reachable=True,
                latency_ms=(time.monotonic() - started) * 1000,
                engines=[],
                last_error=f"probe failed: {exc}",
            )
        latency_ms = (time.monotonic() - started) * 1000

        meta = probe_payload.get("meta") or {}
        responded = set(meta.get("engines_responded") or [])
        errors = {e.get("engine"): e.get("error", "") for e in meta.get("engine_errors") or []}
        engines: list[EngineHealth] = []
        for name in DEFAULT_ENGINES:
            if name in responded:
                engines.append(EngineHealth(name=name, status="ok"))
            elif name in errors:
                engines.append(EngineHealth(name=name, status="degraded", reason=_label(errors[name])))
            else:
                engines.append(EngineHealth(name=name, status="unknown"))

        return BackendHealth(
            backend=self.name, reachable=True, latency_ms=latency_ms, engines=engines, last_error=overall_note
        )
