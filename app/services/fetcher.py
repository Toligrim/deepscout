from __future__ import annotations

import hashlib
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import settings
from ..utils import normalize_url


async def fetch_page(url: str) -> dict:
    url = normalize_url(url)
    async with httpx.AsyncClient(
        timeout=settings.timeout,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
    content_type = r.headers.get("content-type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return {
            "url": str(r.url),
            "status": r.status_code,
            "mime": content_type.split(";", 1)[0],
            "title": None,
            "text": "",
            "links": [],
            "content_hash": hashlib.sha256(r.content).hexdigest(),
        }
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = "\n".join(line.strip() for line in main.get_text("\n").splitlines() if line.strip())
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        try:
            absolute = normalize_url(urljoin(str(r.url), href))
        except ValueError:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append({"url": absolute, "anchor": a.get_text(" ", strip=True)[:300]})
    return {
        "url": normalize_url(str(r.url)),
        "status": r.status_code,
        "mime": content_type.split(";", 1)[0],
        "title": title,
        "text": text,
        "links": links,
        "content_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
    }
