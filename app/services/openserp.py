from __future__ import annotations

import time
from datetime import date, timedelta

import httpx

from ..config import settings
from .providers import BackendHealth, EngineHealth, SearchResponse, SearchResult

# Baidu is opt-in only (not part of the RU/EN workflow) and excluded from the default set.
# Whichever engines are queried, DeepScout never tries to defeat a CAPTCHA/block or otherwise
# evade anti-bot protection — an engine failing that way is just reported as degraded.
DEFAULT_ENGINES = ["google", "bing", "yandex", "duckduckgo", "ecosia"]
ALL_ENGINES = ["google", "bing", "yandex", "duckduckgo", "ecosia", "baidu"]

# Result page size for /mega/search (its own default is 10; DeepScout asks for a page size
# closer to what SearXNG typically returns per page).
DEFAULT_LIMIT = 20

# SearXNG-style time_range -> how many days back from today, for OpenSERP's `date`
# param (YYYYMMDD..YYYYMMDD, confirmed against the live openapi.yaml).
_TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# OpenSERP v0.8.12 error codes (see /mega/search meta.engine_errors[].error and the
# ErrorResponse.error enum in its OpenAPI spec), mapped to DeepScout's own short
# health-reason vocabulary.
_ERROR_LABELS = {
    "captcha_detected": "CAPTCHA",
    "blocked": "blocked",
    "rate_limited": "rate_limited",
    "proxy_timeout": "timeout",
    "timeout": "timeout",
    "search_timeout": "timeout",
    "request_timeout": "timeout",
    "empty_result": "empty_result",
    "engine_internal": "error",
    "parser_failure": "error",
    "request_canceled": "error",
    "proxy_connect": "error",
    "proxy_auth": "error",
    "proxy_unavailable": "unavailable",
    "circuit_open": "unavailable",
    "all_engines_failed": "unavailable",
}


def _label(error_code: str) -> str:
    return _ERROR_LABELS.get(error_code, error_code[:40] or "error")


def _date_param(time_range: str | None, *, today: date | None = None) -> str | None:
    days = _TIME_RANGE_DAYS.get(time_range or "")
    if not days:
        return None
    end = today or date.today()
    start = end - timedelta(days=days)
    return f"{start:%Y%m%d}..{end:%Y%m%d}"


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
        params: dict[str, str | int] = {
            "text": query,
            "engines": ",".join(engines),
            "limit": DEFAULT_LIMIT,
            "start": max(page - 1, 0) * DEFAULT_LIMIT,
            # dedupe=false so every engine's own copy of a shared URL stays in the flat
            # `results` list (needed below to resolve snippet/title for every occurrence
            # in a cluster) — merge=true so all engines' hits are actually present.
            "dedupe": "false",
            "merge": "true",
        }
        if language and language not in {"all", "any"}:
            params["lang"] = language
        date_param = _date_param(time_range)
        if date_param:
            params["date"] = date_param

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=settings.openserp_timeout,
                headers={"User-Agent": settings.user_agent},
            ) as client:
                response = await client.get(f"{settings.openserp_url}/mega/search", params=params)
                payload = response.json()
                if response.status_code >= 400:
                    detail = payload.get("message") or payload.get("error") or f"HTTP {response.status_code}"
                    return SearchResponse(backend=self.name, status="failed", results=[], error=detail)
        except httpx.HTTPError as exc:
            return SearchResponse(backend=self.name, status="failed", results=[], error=str(exc))
        except ValueError as exc:  # malformed JSON body
            return SearchResponse(backend=self.name, status="failed", results=[], error=f"invalid response: {exc}")
        latency_ms = (time.monotonic() - started) * 1000

        # `clusters` (always present on /mega/search, independent of the `dedupe` flag)
        # is the source of truth for cross-engine provenance: the flat `results` list
        # only ever attributes a URL to whichever single engine's copy survived, which
        # loses exactly the multi-engine information DeepScout needs.
        results_by_id = {item.get("id"): item for item in payload.get("results", []) if item.get("id")}
        results: list[SearchResult] = []
        for cluster in payload.get("clusters") or []:
            url = cluster.get("canonical_url")
            occurrences = cluster.get("occurrences") or []
            if not url or not occurrences:
                continue
            best = min(occurrences, key=lambda o: o.get("rank", 999))
            best_item = results_by_id.get(best.get("result_id")) or {}
            if best_item.get("type") == "ad":
                continue
            engine_names = [o["engine"] for o in occurrences if o.get("engine")]
            results.append(
                SearchResult(
                    url=url,
                    title=cluster.get("title") or best_item.get("title"),
                    snippet=best_item.get("snippet"),
                    backend=self.name,
                    engines=engine_names,
                    rank=cluster.get("best_rank") or best.get("rank") or 999,
                    metadata={"domain": cluster.get("domain"), "cluster_score": cluster.get("score")},
                )
            )

        warnings = [
            f"{err.get('engine')}: {_label(err.get('error', ''))}"
            for err in (payload.get("meta") or {}).get("engine_errors", [])
        ]
        status = "degraded" if warnings else "ok"
        return SearchResponse(
            backend=self.name, status=status, results=results, latency_ms=latency_ms, warnings=warnings
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
