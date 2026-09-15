from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SearchResult:
    """Normalized result shape shared by every search backend."""

    url: str
    title: str | None
    snippet: str | None
    backend: str
    engines: list[str]
    rank: int
    score: float | None = None
    published_at: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class SearchResponse:
    backend: str
    status: str  # "ok" | "failed"
    results: list[SearchResult]
    error: str | None = None
    latency_ms: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class EngineHealth:
    name: str
    status: str  # "ok" | "degraded" | "failed" | "unknown"
    reason: str | None = None


@dataclass
class BackendHealth:
    backend: str
    reachable: bool
    latency_ms: float | None
    engines: list[EngineHealth]
    last_error: str | None = None


class SearchProvider(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        page: int = 1,
        language: str = "all",
        time_range: str | None = None,
        engines: list[str] | None = None,
    ) -> SearchResponse: ...

    async def health(self) -> BackendHealth: ...
