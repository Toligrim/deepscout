from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
}


def normalize_domain(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("domain is empty")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().strip(".")
    if not host:
        raise ValueError("invalid domain")
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("url is empty")
    if value.startswith("//"):
        value = "https:" + value
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported url scheme")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("invalid url")
    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    query_pairs = []
    for key, val in parse_qsl(parsed.query, keep_blank_values=True):
        low = key.lower()
        if low.startswith("utm_") or low in TRACKING_PARAMS:
            continue
        query_pairs.append((key, val))
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))


def url_domain(value: str) -> str:
    return normalize_domain(value)


def url_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def classify_url(url: str, mime: str | None = None) -> str:
    mime = (mime or "").lower()
    path = urlparse(url).path.lower()
    if "pdf" in mime or path.endswith(".pdf"):
        return "pdf"
    if any(path.endswith(ext) for ext in (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods")):
        return "document"
    if any(path.endswith(ext) for ext in (".zip", ".tar", ".gz", ".7z", ".rar")):
        return "archive"
    if mime.startswith("image/") or any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return "image"
    return "page"
