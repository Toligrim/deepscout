from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from .. import db as store
from ..config import settings
from ..utils import url_domain
from .aggregation import MergedResult, persist_merged_result, run_search
from .discovery import discover_commoncrawl, discover_sitemaps, discover_wayback
from .relevance import score_relevance

DEFAULT_SOURCES = ["sitemap", "wayback", "commoncrawl"]
ALLOWED_SOURCES = {"sitemap", "wayback", "commoncrawl"}

_SOURCE_CALLS = {
    "sitemap": discover_sitemaps,
    "wayback": discover_wayback,
    "commoncrawl": discover_commoncrawl,
}

_COUNTER_KEYS = (
    "serp_raw",
    "serp_unique",
    "domains_selected",
    "sitemap_urls",
    "wayback_urls",
    "commoncrawl_urls",
    "unique_urls",
    "high_relevance",
    "possible_relevance",
    "discovered_relevance",
)

_LOG_CAP = 60


@dataclass
class Progress:
    phase: str = "queued"
    counters: dict = field(default_factory=lambda: {k: 0 for k in _COUNTER_KEYS})
    log: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(message)
        del self.log[:-_LOG_CAP]

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def to_json(self) -> str:
        return json.dumps(
            {"phase": self.phase, "counters": self.counters, "log": self.log, "warnings": self.warnings},
            ensure_ascii=False,
        )


def _push(job_id: int, progress: Progress) -> None:
    store.update_job(job_id, progress_json=progress.to_json())


# A process-global gate on discovery concurrency, shared by every Deep Search job
# running in this process — not one Semaphore per job (which would let N concurrent
# jobs each run DEEPSCOUT_DEEP_SEARCH_CONCURRENCY discovery calls, N times the intended
# total load). Lazily created against whatever event loop is currently running: a
# Semaphore created before the loop exists (e.g. at import time) — or on a now-dead
# loop from a previous asyncio.run() in tests — can't be awaited safely, so this
# recreates it whenever the running loop differs from the one it was built for. In
# production (one persistent uvicorn worker, one long-lived loop) this still means:
# created once, reused by every job for the process's whole lifetime.
_global_semaphore: asyncio.Semaphore | None = None
_global_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_global_semaphore() -> asyncio.Semaphore:
    global _global_semaphore, _global_semaphore_loop
    loop = asyncio.get_running_loop()
    if _global_semaphore is None or _global_semaphore_loop is not loop:
        _global_semaphore = asyncio.Semaphore(max(settings.deep_search_concurrency, 1))
        _global_semaphore_loop = loop
    return _global_semaphore


def score_domain(urls: list[MergedResult], query: str) -> tuple[float, dict]:
    """Deterministic, documented, unit-tested — the four SERP-signal factors plus one
    small lexical check: best (lowest) rank any of the domain's URLs achieved, how many
    of its URLs made the SERP cut, how many distinct engines/backends corroborated it,
    and how well the domain's own best SERP hit actually matches the query lexically.

    score = 1/best_rank + 0.1*min(url_count, 5) + 0.3*distinct_engines + 0.5*(backends > 1)
          + 0.3*domain_query_relevance

    The lexical term is intentionally small (max +0.3, vs. the SERP corroboration terms
    which commonly add up to 1+) — it's a tiebreak-strength nudge against a domain that
    only looks strong because one engine returned a lot of noise, not a veto. A domain
    is never excluded just because domain_query_relevance is 0; it only loses a small
    edge against otherwise-similar competitors.
    """
    best_rank = min(u.best_rank for u in urls)
    engines = sorted({e for u in urls for e in u.engines})
    backends = sorted({b for u in urls for b in u.backends})
    url_count = len(urls)
    domain_query_relevance = max(
        (score_relevance(query, u.canonical_url, u.title, u.snippet).score for u in urls), default=0.0
    )
    score = 1.0 / max(best_rank, 1) + 0.1 * min(url_count, 5) + 0.3 * len(engines)
    score += 0.5 if len(backends) > 1 else 0.0
    score += 0.3 * domain_query_relevance
    explanation = {
        "best_rank": best_rank,
        "url_count": url_count,
        "engines": engines,
        "backends": backends,
        "domain_query_relevance": round(domain_query_relevance, 4),
    }
    return round(score, 4), explanation


def select_domains(
    results: list[MergedResult], max_domains: int, query: str
) -> list[tuple[str, float, dict, list[MergedResult]]]:
    by_domain: dict[str, list[MergedResult]] = {}
    for m in results:
        try:
            domain = url_domain(m.canonical_url)
        except ValueError:
            continue
        by_domain.setdefault(domain, []).append(m)

    scored = []
    for domain, urls in by_domain.items():
        score, explanation = score_domain(urls, query)
        scored.append((domain, score, explanation, urls))
    # deterministic: score desc, ties broken by domain name so re-runs on the same
    # input always produce the same Top N in the same order
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:max_domains]


async def _run_discovery(
    domain: str,
    source: str,
    limit: int,
    semaphore: asyncio.Semaphore,
    *,
    project_id: int,
    progress: Progress,
    job_id: int,
    discovered: dict[str, dict],
    errors: list[dict],
) -> None:
    async with semaphore:
        try:
            items = await _SOURCE_CALLS[source](domain, limit)
            error = None
        except Exception as exc:  # each (domain, source) call is isolated from every other
            items, error = [], str(exc)

    accepted = 0
    for item in items:
        url = item.get("url", "")
        status = item.get("status")
        source_detail = item.get("source_detail") or item.get("collection")
        row = store.upsert_url(
            project_id,
            url,
            source=source,
            source_detail=source_detail,
            mime=item.get("mime"),
            live_status=int(status) if str(status or "").isdigit() else None,
        )
        if not row:
            continue
        accepted += 1
        if source in {"wayback", "commoncrawl"}:
            store.add_capture(
                row["id"], source, item.get("timestamp"),
                int(status) if str(status or "").isdigit() else None,
                item.get("mime"), item.get("digest"), item.get("raw") or item,
            )
        store.add_job_url_source(job_id, row["id"], source, source_detail)
        # a URL discovery finds again after already appearing in SERP keeps its SERP
        # rank — discovery itself carries no ranking signal, so it must never clear one
        previous_rank = discovered.get(row["url"], {}).get("best_serp_rank")
        discovered[row["url"]] = {
            "url_id": row["id"], "title": row["title"], "snippet": row["snippet"],
            "best_serp_rank": previous_rank,
        }

    progress.counters[f"{source}_urls"] += accepted
    if error:
        progress.warn(f"{domain}: {source}: {error}")
        errors.append({"domain": domain, "source": source, "error": error})
    progress.note(f"{source}: {domain} ({accepted})")
    _push(job_id, progress)


async def run_deep_search_job(job_id: int, project_id: int, query: str, params: dict) -> None:
    """The whole Deep Search pipeline: SERP -> domain selection -> discovery ->
    relevance -> merge. Every phase persists through the existing urls/url_sources/
    captures tables (same functions the rest of the app already uses); job_urls only
    links an already-stored URL to this job with its relevance. A failure anywhere
    inside one (domain, source) discovery call is isolated there; only a genuinely
    unexpected exception outside that isolation reaches the outer except below.
    """
    started = time.monotonic()
    progress = Progress()
    errors: list[dict] = []
    domains: list[tuple[str, float, dict, list[MergedResult]]] = []
    aggregated_backends: dict = {}
    store.update_job(job_id, status="running", started_at=store.utcnow())

    try:
        # Phase A — SERP
        progress.phase = "searching"
        progress.note("Searching web")
        _push(job_id, progress)

        aggregated = await run_search(query)
        aggregated_backends = aggregated.backends
        kept = aggregated.results[: params["max_serp_results"]]
        progress.counters["serp_raw"] = sum(b["count"] for b in aggregated.backends.values())
        progress.counters["serp_unique"] = len(kept)
        for w in aggregated.warnings:
            progress.warn(f"serp: {w}")

        discovered: dict[str, dict] = {}
        for m in kept:
            row = persist_merged_result(project_id, m)
            if not row:
                continue
            for backend, engine in m.contributions:
                store.add_job_url_source(job_id, row["id"], backend, engine)
            discovered[row["url"]] = {
                "url_id": row["id"], "title": row["title"], "snippet": row["snippet"],
                "best_serp_rank": m.best_rank,
            }

        progress.note(f"{progress.counters['serp_raw']} SERP results, {progress.counters['serp_unique']} unique")
        _push(job_id, progress)

        # Phase B — domain selection
        progress.phase = "selecting_domains"
        progress.note("Selecting domains")
        _push(job_id, progress)

        domains = select_domains(kept, params["max_domains"], query)
        progress.counters["domains_selected"] = len(domains)
        progress.note(f"{len(domains)} domains selected")
        _push(job_id, progress)

        # Phase C — discovery (bounded concurrency, each (domain, source) isolated)
        progress.phase = "discovering"
        sources = params["sources"]
        if domains and sources:
            semaphore = _get_global_semaphore()
            await asyncio.gather(
                *(
                    _run_discovery(
                        domain, source, params["limit_per_source"], semaphore,
                        project_id=project_id, progress=progress, job_id=job_id,
                        discovered=discovered, errors=errors,
                    )
                    for domain, _score, _explanation, _urls in domains
                    for source in sources
                )
            )

        # Phase D + E — relevance scoring and job_urls merge
        progress.phase = "scoring"
        progress.note("Scoring relevance")
        _push(job_id, progress)

        domain_scores = {domain: score for domain, score, _explanation, _urls in domains}
        for url, info in discovered.items():
            try:
                domain = url_domain(url)
            except ValueError:
                domain = None
            result = score_relevance(query, url, info.get("title"), info.get("snippet"))
            store.add_job_url(
                job_id, info["url_id"], result.score, result.tier,
                domain_scores.get(domain), info.get("best_serp_rank"), {"query": query},
            )
            progress.counters["unique_urls"] += 1
            progress.counters[f"{result.tier}_relevance"] += 1

        total_unique = progress.counters["unique_urls"]
        has_problems = bool(progress.warnings) or bool(errors)
        if total_unique == 0:
            status = "failed"
            error_msg = "; ".join(progress.warnings[:5]) or "no usable results"
        elif has_problems:
            status, error_msg = "partial", None
        else:
            status, error_msg = "completed", None

        progress.phase = "done"
        progress.note({"completed": "Completed", "partial": "Partial", "failed": "Failed"}[status])
        _push(job_id, progress)

        summary = {
            "status": status,
            "counters": progress.counters,
            "domains": [{"domain": d, "score": s, "explanation": e} for d, s, e, _u in domains],
            "serp_backends": aggregated_backends,
            "discovery_errors": errors,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        }
        store.update_job(
            job_id, status=status, result_summary_json=json.dumps(summary, ensure_ascii=False),
            error=error_msg, finished_at=store.utcnow(),
        )
    except Exception as exc:  # a genuine internal bug, not a source-level failure
        progress.warn(f"internal error: {exc}")
        _push(job_id, progress)
        store.update_job(job_id, status="failed", error=str(exc), finished_at=store.utcnow())
