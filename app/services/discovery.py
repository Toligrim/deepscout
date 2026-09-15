from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..utils import normalize_domain, normalize_url, url_domain

COMMONCRAWL_COLLECTIONS_TO_TRY = 5
COMMONCRAWL_MAX_RETRIES = 3
COMMONCRAWL_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
COMMONCRAWL_BACKOFF_BASE = 1.0
COMMONCRAWL_BACKOFF_CAP = 8.0
COMMONCRAWL_BACKOFF_JITTER = 0.5


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    )


async def discover_sitemaps(domain: str, limit: int = 5000) -> list[dict]:
    domain = normalize_domain(domain)
    candidates = {f"https://{domain}/sitemap.xml", f"http://{domain}/sitemap.xml"}
    results: list[dict] = []
    seen_sitemaps: set[str] = set()
    async with _client() as client:
        for scheme in ("https", "http"):
            robots_url = f"{scheme}://{domain}/robots.txt"
            try:
                r = await client.get(robots_url)
                if r.status_code == 200:
                    for line in r.text.splitlines():
                        if line.lower().startswith("sitemap:"):
                            candidates.add(line.split(":", 1)[1].strip())
                    break
            except httpx.HTTPError:
                pass

        queue = deque(candidates)
        while queue and len(results) < limit and len(seen_sitemaps) < 50:
            sitemap_url = queue.popleft()
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)
            try:
                r = await client.get(sitemap_url)
                if r.status_code != 200 or len(r.content) > 20_000_000:
                    continue
                soup = BeautifulSoup(r.content, "xml")
            except Exception:
                continue
            root_name = soup.find().name.lower() if soup.find() else ""
            locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
            if root_name == "sitemapindex":
                for loc in locs:
                    if loc not in seen_sitemaps:
                        queue.append(loc)
                continue
            for loc in locs:
                if len(results) >= limit:
                    break
                try:
                    normalized = normalize_url(loc)
                except ValueError:
                    continue
                if url_domain(normalized) == domain or url_domain(normalized).endswith("." + domain):
                    results.append({"url": normalized, "source_detail": sitemap_url})
    return results


async def discover_wayback(domain: str, limit: int = 2000, include_subdomains: bool = True) -> list[dict]:
    domain = normalize_domain(domain)
    params = {
        "url": domain,
        "matchType": "domain" if include_subdomains else "host",
        "output": "json",
        "fl": "timestamp,original,mimetype,statuscode,digest",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "limit": str(min(limit, 10000)),
    }
    async with _client() as client:
        r = await client.get("https://web.archive.org/cdx/search/cdx", params=params)
        r.raise_for_status()
        data = r.json()
    if not data or len(data) < 2:
        return []
    headers = data[0]
    results = []
    for row in data[1:]:
        item = dict(zip(headers, row))
        url = item.get("original")
        if not url:
            continue
        results.append({
            "url": url,
            "timestamp": item.get("timestamp"),
            "mime": item.get("mimetype"),
            "status": item.get("statuscode"),
            "digest": item.get("digest"),
            "raw": item,
        })
    return results


def _commoncrawl_backoff_delay(attempt: int) -> float:
    base = min(COMMONCRAWL_BACKOFF_BASE * (2 ** (attempt - 1)), COMMONCRAWL_BACKOFF_CAP)
    return base + random.uniform(0, COMMONCRAWL_BACKOFF_JITTER)


def _parse_cdx_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("url"):
            rows.append(item)
    return rows


async def _query_commoncrawl_collection(
    client: httpx.AsyncClient,
    endpoint: str,
    domain: str,
    limit: int,
    *,
    sleep=asyncio.sleep,
) -> tuple[list[dict], str | None]:
    """Query a single CC collection. Returns (rows, error); error is None on success
    (an empty row list with no error means the collection simply has no matches).
    429/500/502/503/504 get a bounded retry with exponential backoff + jitter;
    400 and other unexpected statuses are treated as non-retryable for this collection.
    """
    params = {
        "url": f"{domain}/*",
        "output": "json",
        "filter": "status:200",
        "collapse": "urlkey",
        "limit": str(min(limit, 10000)),
    }
    attempt = 0
    while True:
        try:
            r = await client.get(endpoint, params=params)
        except httpx.TransportError as exc:
            attempt += 1
            if attempt > COMMONCRAWL_MAX_RETRIES:
                return [], f"transport error after {attempt - 1} retries: {exc}"
            await sleep(_commoncrawl_backoff_delay(attempt))
            continue

        if r.status_code == 200:
            return _parse_cdx_lines(r.text), None
        if r.status_code == 404:
            # CC returns 404 when a collection simply has no index shard for this query
            return [], None
        if r.status_code == 400:
            return [], f"HTTP 400 (bad request, not retried): {r.text[:200]!r}"
        if r.status_code in COMMONCRAWL_RETRYABLE_STATUS:
            attempt += 1
            if attempt > COMMONCRAWL_MAX_RETRIES:
                return [], f"HTTP {r.status_code} after {attempt - 1} retries"
            await sleep(_commoncrawl_backoff_delay(attempt))
            continue
        return [], f"HTTP {r.status_code}"


async def discover_commoncrawl(domain: str, limit: int = 2000, *, sleep=asyncio.sleep) -> list[dict]:
    domain = normalize_domain(domain)
    async with _client() as client:
        coll_resp = await client.get("https://index.commoncrawl.org/collinfo.json")
        coll_resp.raise_for_status()
        collections = coll_resp.json()
        if not collections:
            raise RuntimeError("Common Crawl: collinfo.json returned no collections")

        candidates = collections[:COMMONCRAWL_COLLECTIONS_TO_TRY]
        seen_urls: set[str] = set()
        results: list[dict] = []
        errors: list[str] = []

        for coll in candidates:
            if len(results) >= limit:
                break
            coll_id = coll.get("id", "unknown")
            endpoint = coll.get("cdx-api")
            if not endpoint:
                errors.append(f"{coll_id}: no cdx-api endpoint")
                continue

            rows, error = await _query_commoncrawl_collection(
                client, endpoint, domain, limit, sleep=sleep
            )
            if error:
                errors.append(f"{coll_id}: {error}")
                continue

            for item in rows:
                if len(results) >= limit:
                    break
                url = item.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                results.append({
                    "url": url,
                    "timestamp": item.get("timestamp"),
                    "mime": item.get("mime") or item.get("mime-detected"),
                    "status": item.get("status"),
                    "digest": item.get("digest"),
                    "raw": item,
                    "collection": coll_id,
                })

        if not results and errors and len(errors) >= len(candidates):
            raise RuntimeError("Common Crawl: all collections failed: " + "; ".join(errors))

        return results


async def _robots_parser(client: httpx.AsyncClient, origin: str) -> RobotFileParser | None:
    robots_url = f"{origin}/robots.txt"
    try:
        r = await client.get(robots_url)
        if r.status_code >= 400:
            return None
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.parse(r.text.splitlines())
        return rp
    except Exception:
        return None


async def discover_live_crawl(domain: str, limit: int = 200, depth: int = 1) -> list[dict]:
    domain = normalize_domain(domain)
    seeds = [f"https://{domain}/", f"http://{domain}/"]
    seen: set[str] = set()
    results: list[dict] = []
    queue: deque[tuple[str, int]] = deque((s, 0) for s in seeds)

    async with _client() as client:
        robots_cache: dict[str, RobotFileParser | None] = {}
        while queue and len(results) < limit:
            url, level = queue.popleft()
            try:
                url = normalize_url(url)
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            if url_domain(url) != domain:
                continue
            origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            if origin not in robots_cache:
                robots_cache[origin] = await _robots_parser(client, origin)
            rp = robots_cache[origin]
            if rp is not None and not rp.can_fetch(settings.user_agent, url):
                continue
            try:
                r = await client.get(url)
            except httpx.HTTPError:
                continue
            if r.status_code >= 400:
                continue
            ctype = r.headers.get("content-type", "")
            results.append({"url": str(r.url), "status": r.status_code, "mime": ctype.split(";", 1)[0], "source_detail": f"depth:{level}"})
            if level >= depth or "text/html" not in ctype:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a.get("href", "").strip()
                if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                absolute = urljoin(str(r.url), href)
                try:
                    candidate = normalize_url(absolute)
                except ValueError:
                    continue
                if url_domain(candidate) == domain and candidate not in seen:
                    queue.append((candidate, level + 1))
            await asyncio.sleep(0.05)
    return results
