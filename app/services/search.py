from __future__ import annotations

import httpx

from ..config import settings


async def search_searxng(
    query: str,
    *,
    page: int = 1,
    language: str = "all",
    time_range: str | None = None,
) -> list[dict]:
    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "pageno": max(page, 1),
        "language": language or "all",
        "safesearch": 0,
    }
    if time_range in {"day", "week", "month", "year"}:
        params["time_range"] = time_range
    async with httpx.AsyncClient(
        timeout=settings.timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        response = await client.get(f"{settings.searxng_url}/search", params=params)
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in payload.get("results", []):
        url = item.get("url")
        if not url:
            continue
        results.append(
            {
                "url": url,
                "title": item.get("title"),
                "snippet": item.get("content") or item.get("snippet"),
                "engines": item.get("engines") or ([item.get("engine")] if item.get("engine") else []),
                "score": item.get("score"),
                "publishedDate": item.get("publishedDate"),
            }
        )
    return results
