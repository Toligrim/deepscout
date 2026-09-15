from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..utils import normalize_url
from .openserp import OpenSerpProvider
from .providers import SearchProvider, SearchResponse
from .search import SearxngProvider

PROVIDERS: dict[str, SearchProvider] = {
    "searxng": SearxngProvider(),
    "openserp": OpenSerpProvider(),
}


@dataclass
class MergedResult:
    canonical_url: str
    title: str | None
    snippet: str | None
    backends: list[str]
    engines: list[str]
    best_rank: int
    score: float
    published_at: str | None = None
    metadata: dict = field(default_factory=dict)
    # deduped (backend, engine) pairs actually observed, for accurate provenance rows —
    # `engines` above is a flattened, backend-agnostic list meant for display/ranking only
    contributions: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class AggregatedSearchResponse:
    query: str
    results: list[MergedResult]
    backends: dict[str, dict]
    warnings: list[str]


def _score(best_rank: int, engine_count: int, backend_count: int, searxng_score: float | None) -> float:
    """Deterministic ranking, documented so it stays testable:

    score = 1/best_rank                              -- reward a good position in *any* contributor
          + 0.3 * (distinct_engine_count - 1)         -- corroboration across engines
          + 0.5 * (distinct_backend_count > 1)        -- corroboration across backends
          + 0.1 * searxng_score (if present)          -- fold in SearXNG's own relevance score

    `best_rank` is the best (lowest) per-provider rank across every contributing hit —
    SearXNG's rank is already cross-engine (it merges internally), OpenSERP's rank is
    per-engine (via /mega/search); both are monotonic "better is lower", which is all
    this formula needs.
    """
    s = 1.0 / max(best_rank, 1)
    s += 0.3 * max(engine_count - 1, 0)
    s += 0.5 if backend_count > 1 else 0.0
    if searxng_score:
        s += 0.1 * searxng_score
    return s


async def run_search(
    query: str,
    *,
    backends: list[str] | None = None,
    openserp_engines: list[str] | None = None,
    page: int = 1,
    language: str = "all",
    time_range: str | None = None,
) -> AggregatedSearchResponse:
    selected = [b for b in (backends or list(PROVIDERS)) if b in PROVIDERS]
    if not selected:
        selected = list(PROVIDERS)

    async def run_one(name: str) -> SearchResponse:
        provider = PROVIDERS[name]
        try:
            if name == "openserp":
                return await provider.search(
                    query, page=page, language=language, time_range=time_range, engines=openserp_engines
                )
            return await provider.search(query, page=page, language=language, time_range=time_range)
        except Exception as exc:  # a bug in one provider must never break the others
            return SearchResponse(backend=name, status="failed", results=[], error=str(exc))

    responses = await asyncio.gather(*(run_one(name) for name in selected))

    merged: dict[str, MergedResult] = {}
    for response in responses:
        for hit in response.results:
            try:
                canonical = normalize_url(hit.url)
            except ValueError:
                continue
            hit_pairs = [(hit.backend, e) for e in hit.engines] or [(hit.backend, None)]
            existing = merged.get(canonical)
            if existing is None:
                merged[canonical] = MergedResult(
                    canonical_url=canonical,
                    title=hit.title,
                    snippet=hit.snippet,
                    backends=[hit.backend],
                    engines=list(dict.fromkeys(hit.engines)),
                    best_rank=hit.rank,
                    score=0.0,
                    published_at=hit.published_at,
                    metadata={"searxng_score": hit.score if hit.backend == "searxng" else None},
                    contributions=hit_pairs,
                )
                continue
            if hit.backend not in existing.backends:
                existing.backends.append(hit.backend)
            for e in hit.engines:
                if e not in existing.engines:
                    existing.engines.append(e)
            for pair in hit_pairs:
                if pair not in existing.contributions:
                    existing.contributions.append(pair)
            # prefer the contributing hit with the best (lowest) rank for title/snippet;
            # ties broken by whichever has the longer non-empty snippet
            is_better_rank = hit.rank < existing.best_rank
            if is_better_rank:
                existing.best_rank = hit.rank
            longer_snippet = (not existing.snippet) or (hit.snippet and len(hit.snippet) > len(existing.snippet or ""))
            if hit.title and (is_better_rank or not existing.title):
                existing.title = hit.title
            if hit.snippet and (is_better_rank or longer_snippet):
                existing.snippet = hit.snippet
            if hit.backend == "searxng" and hit.score:
                existing.metadata["searxng_score"] = hit.score
            existing.published_at = existing.published_at or hit.published_at

    results = []
    for m in merged.values():
        m.score = _score(m.best_rank, len(m.engines), len(m.backends), m.metadata.get("searxng_score"))
        results.append(m)
    results.sort(key=lambda m: (-m.score, m.canonical_url))

    backend_summary = {}
    warnings: list[str] = []
    for response in responses:
        backend_summary[response.backend] = {
            "status": response.status,
            "count": len(response.results),
            "error": response.error,
        }
        if response.status == "failed":
            warnings.append(f"{response.backend}: {response.error}")
        for w in response.warnings:
            warnings.append(f"{response.backend}: {w}")

    return AggregatedSearchResponse(query=query, results=results, backends=backend_summary, warnings=warnings)
