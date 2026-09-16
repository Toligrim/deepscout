from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db as store
from .config import settings
from .services.aggregation import persist_merged_result, run_search
from .services.deep_search import ALLOWED_SOURCES, DEFAULT_SOURCES, run_deep_search_job
from .services.discovery import (
    discover_commoncrawl,
    discover_live_crawl,
    discover_sitemaps,
    discover_wayback,
)
from .services.fetcher import fetch_page
from .services.health import get_search_health
from .utils import normalize_domain

# Deep Search jobs run as fire-and-forget asyncio.create_task()s; keeping a strong
# reference here stops them from being garbage-collected mid-run (a well-known asyncio
# gotcha for tasks nothing else holds onto).
_background_tasks: set[asyncio.Task] = set()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="DeepScout", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SearchRequest(BaseModel):
    project_id: int = 1
    query: str = Field(min_length=1, max_length=1000)
    page: int = 1
    language: str = "all"
    time_range: str | None = None
    backends: list[str] | None = None
    openserp_engines: list[str] | None = None


class DiscoverRequest(BaseModel):
    project_id: int = 1
    domain: str
    sources: list[str] = ["sitemap", "wayback", "commoncrawl"]
    limit_per_source: int = Field(default=1000, ge=1, le=10000)
    include_subdomains: bool = True
    crawl_depth: int = Field(default=1, ge=0, le=3)


class FetchRequest(BaseModel):
    project_id: int = 1
    url: str


class DeepSearchRequest(BaseModel):
    project_id: int = 1
    query: str = Field(min_length=1, max_length=1000)
    max_serp_results: int = Field(default=50, ge=1, le=200)
    max_domains: int = Field(default=10, ge=1, le=25)
    limit_per_source: int = Field(default=100, ge=1, le=1000)
    sources: list[str] = list(DEFAULT_SOURCES)


@app.on_event("startup")
def startup() -> None:
    store.init_db()
    store.mark_interrupted_jobs()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": "0.1.0",
        "searxng_url": settings.searxng_url,
        "db": str(settings.db_path),
    }


@app.get("/api/projects")
def projects():
    return store.list_projects()


@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    return store.create_project(payload.name)


@app.post("/api/search")
async def web_search(payload: SearchRequest):
    # No health precheck here on purpose: every configured backend is tried directly,
    # in parallel, on every request. run_search()/aggregation.py already isolate a
    # failing backend (or a failing provider.search() call) from the others — a health
    # probe first would just add latency and an extra round-trip for no benefit. Health
    # state is only for the UI, via the separate GET /api/search/health.
    aggregated = await run_search(
        payload.query,
        backends=payload.backends,
        openserp_engines=payload.openserp_engines,
        page=payload.page,
        language=payload.language,
        time_range=payload.time_range,
    )

    stored = []
    for m in aggregated.results:
        row = persist_merged_result(payload.project_id, m)
        if not row:
            continue
        stored.append(
            {
                "url": m.canonical_url,
                "canonical_url": row["url"],
                "title": m.title,
                "snippet": m.snippet,
                "backends": m.backends,
                "engines": m.engines,
                "rank": m.best_rank,
                "score": round(m.score, 4),
                "publishedDate": m.published_at,
            }
        )

    for backend in aggregated.backends:
        store.record_query(payload.project_id, payload.query, backend, aggregated.backends[backend]["count"])

    return {
        "query": payload.query,
        "count": len(stored),
        "results": stored,
        "backends": aggregated.backends,
        "warnings": aggregated.warnings,
    }


@app.get("/api/search/health")
async def search_health(refresh: bool = False):
    return await get_search_health(force=refresh)


def _job_view(job: dict) -> dict:
    return {
        "id": job["id"],
        "project_id": job["project_id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": json.loads(job["progress_json"]) if job["progress_json"] else None,
        "result_summary": json.loads(job["result_summary_json"]) if job["result_summary_json"] else None,
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


@app.post("/api/deep-search")
async def create_deep_search(payload: DeepSearchRequest):
    unknown = [s for s in payload.sources if s not in ALLOWED_SOURCES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown source(s): {', '.join(unknown)}")
    sources = payload.sources
    if not sources:
        raise HTTPException(status_code=400, detail="No sources selected")
    params = {
        "max_serp_results": payload.max_serp_results,
        "max_domains": payload.max_domains,
        "limit_per_source": payload.limit_per_source,
        "sources": sources,
        "query": payload.query,
    }
    job = store.create_job(payload.project_id, "deep_search", params)
    task = asyncio.create_task(run_deep_search_job(job["id"], payload.project_id, payload.query, params))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"job_id": job["id"]}


@app.get("/api/deep-search/{job_id}")
def get_deep_search(job_id: int):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_view(job)


@app.get("/api/deep-search/{job_id}/results")
def get_deep_search_results(
    job_id: int,
    tiers: str = "high,possible",
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
):
    # project_id is deliberately not accepted from the client — the job already knows
    # which project it belongs to, and trusting a client-supplied value here would let
    # one project's results be requested under another project's id.
    if not store.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    tier_list = None if tiers.strip().lower() == "all" else [t.strip() for t in tiers.split(",") if t.strip()]
    return store.list_job_urls(job_id, tiers=tier_list, limit=limit, offset=offset)


_TERMINAL_JOB_STATUSES = {"completed", "partial", "failed", "cancelled", "interrupted"}


@app.get("/api/deep-search/{job_id}/events")
async def deep_search_events(job_id: int):
    async def stream():
        last_snapshot = None
        while True:
            job = store.get_job(job_id)
            if job is None:
                yield f"event: error\ndata: {json.dumps({'detail': 'job not found'})}\n\n"
                return
            snapshot = json.dumps({"status": job["status"], "progress": _job_view(job)["progress"]}, ensure_ascii=False)
            if snapshot != last_snapshot:
                yield f"event: progress\ndata: {snapshot}\n\n"
                last_snapshot = snapshot
            if job["status"] in _TERMINAL_JOB_STATUSES:
                yield f"event: done\ndata: {json.dumps(_job_view(job), ensure_ascii=False)}\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/discover/domain")
async def discover_domain(payload: DiscoverRequest):
    try:
        domain = normalize_domain(payload.domain)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    allowed = {"sitemap", "wayback", "commoncrawl", "crawl"}
    sources = [s for s in payload.sources if s in allowed]
    if not sources:
        raise HTTPException(status_code=400, detail="No valid sources selected")

    async def run(source: str):
        try:
            if source == "sitemap":
                return source, await discover_sitemaps(domain, payload.limit_per_source), None
            if source == "wayback":
                return source, await discover_wayback(domain, payload.limit_per_source, payload.include_subdomains), None
            if source == "commoncrawl":
                return source, await discover_commoncrawl(domain, payload.limit_per_source), None
            if source == "crawl":
                return source, await discover_live_crawl(domain, min(payload.limit_per_source, 1000), payload.crawl_depth), None
        except Exception as exc:  # each provider is isolated
            return source, [], str(exc)
        return source, [], "unsupported source"

    provider_results = await asyncio.gather(*(run(source) for source in sources))
    summary = {}
    added = 0
    for source, items, error in provider_results:
        accepted = 0
        for item in items:
            row = store.upsert_url(
                payload.project_id,
                item.get("url", ""),
                source=source,
                source_detail=item.get("source_detail") or item.get("collection"),
                mime=item.get("mime"),
                live_status=int(item["status"]) if str(item.get("status", "")).isdigit() else None,
            )
            if not row:
                continue
            accepted += 1
            if source in {"wayback", "commoncrawl"}:
                store.add_capture(
                    row["id"],
                    source,
                    item.get("timestamp"),
                    int(item["status"]) if str(item.get("status", "")).isdigit() else None,
                    item.get("mime"),
                    item.get("digest"),
                    item.get("raw") or item,
                )
        added += accepted
        summary[source] = {"found": len(items), "accepted": accepted, "error": error}

    return {
        "domain": domain,
        "providers": summary,
        "processed": added,
        "stats": store.get_stats(payload.project_id),
    }


@app.post("/api/fetch")
async def fetch(payload: FetchRequest):
    row = store.upsert_url(payload.project_id, payload.url, source="manual-fetch")
    if not row:
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        page = await fetch_page(row["url"])
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc
    if page["text"]:
        store.save_page(row["id"], page["title"], page["text"], page["content_hash"])
    linked = 0
    for link in page["links"][:2000]:
        if store.upsert_url(
            payload.project_id,
            link["url"],
            source="link",
            source_detail=row["url"],
            snippet=link.get("anchor") or None,
        ):
            linked += 1
    return {
        "url": page["url"],
        "title": page["title"],
        "status": page["status"],
        "mime": page["mime"],
        "text_preview": page["text"][:8000],
        "links_found": len(page["links"]),
        "links_stored": linked,
    }


@app.get("/api/urls")
def urls(
    project_id: int = 1,
    q: str | None = None,
    source: str | None = None,
    domain: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    return store.list_urls(
        project_id,
        q=q,
        source=source,
        domain=domain,
        kind=kind,
        limit=limit,
        offset=offset,
    )


@app.get("/api/local-search")
def local_search(project_id: int = 1, q: str = Query(min_length=1), limit: int = Query(default=50, ge=1, le=200)):
    try:
        return store.search_local(project_id, q, limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FTS query failed: {exc}") from exc


@app.get("/api/stats")
def stats(project_id: int = 1):
    return store.get_stats(project_id)
