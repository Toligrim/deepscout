from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import settings
from .utils import classify_url, normalize_url, url_domain, url_hash


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                query TEXT NOT NULL,
                provider TEXT NOT NULL,
                result_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                title TEXT,
                snippet TEXT,
                kind TEXT NOT NULL DEFAULT 'page',
                mime TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                live_status INTEGER,
                fetched_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_urls_domain ON urls(domain);
            CREATE INDEX IF NOT EXISTS idx_urls_kind ON urls(kind);

            CREATE TABLE IF NOT EXISTS project_urls (
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
                saved INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                PRIMARY KEY(project_id, url_id)
            );

            CREATE TABLE IF NOT EXISTS url_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                source_detail TEXT,
                discovered_at TEXT NOT NULL,
                UNIQUE(project_id, url_id, source, source_detail)
            );
            CREATE INDEX IF NOT EXISTS idx_url_sources_project ON url_sources(project_id);
            CREATE INDEX IF NOT EXISTS idx_url_sources_source ON url_sources(source);

            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                captured_at TEXT,
                status_code INTEGER,
                mime TEXT,
                digest TEXT,
                raw_json TEXT,
                UNIQUE(url_id, source, captured_at, digest)
            );

            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_id INTEGER NOT NULL UNIQUE REFERENCES urls(id) ON DELETE CASCADE,
                title TEXT,
                text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                title,
                text,
                content='pages',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
                INSERT INTO pages_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
                INSERT INTO pages_fts(pages_fts, rowid, title, text) VALUES('delete', old.id, old.title, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
                INSERT INTO pages_fts(pages_fts, rowid, title, text) VALUES('delete', old.id, old.title, old.text);
                INSERT INTO pages_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
            END;
            """
        )
        row = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()
        if row["n"] == 0:
            conn.execute(
                "INSERT INTO projects(name, created_at) VALUES (?, ?)",
                ("Default", utcnow()),
            )


def list_projects() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def create_project(name: str) -> dict:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO projects(name, created_at) VALUES (?, ?)",
            (name.strip() or "Untitled", utcnow()),
        )
        row = conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


def record_query(project_id: int, query: str, provider: str, result_count: int) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO queries(project_id, query, provider, result_count, created_at) VALUES (?,?,?,?,?)",
            (project_id, query, provider, result_count, utcnow()),
        )


def upsert_url(
    project_id: int,
    url: str,
    *,
    source: str,
    source_detail: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
    mime: str | None = None,
    live_status: int | None = None,
) -> dict | None:
    try:
        canonical = normalize_url(url)
    except ValueError:
        return None
    now = utcnow()
    domain = url_domain(canonical)
    kind = classify_url(canonical, mime)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO urls(url_hash, url, domain, title, snippet, kind, mime, first_seen_at, last_seen_at, live_status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url_hash) DO UPDATE SET
                title=COALESCE(excluded.title, urls.title),
                snippet=COALESCE(excluded.snippet, urls.snippet),
                mime=COALESCE(excluded.mime, urls.mime),
                kind=CASE WHEN excluded.kind != 'page' THEN excluded.kind ELSE urls.kind END,
                last_seen_at=excluded.last_seen_at,
                live_status=COALESCE(excluded.live_status, urls.live_status)
            """,
            (url_hash(canonical), canonical, domain, title, snippet, kind, mime, now, now, live_status),
        )
        row = conn.execute("SELECT * FROM urls WHERE url_hash=?", (url_hash(canonical),)).fetchone()
        url_id = row["id"]
        conn.execute(
            "INSERT OR IGNORE INTO project_urls(project_id, url_id) VALUES (?,?)",
            (project_id, url_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO url_sources(project_id, url_id, source, source_detail, discovered_at) VALUES (?,?,?,?,?)",
            (project_id, url_id, source, source_detail, now),
        )
        return dict(row)


def add_url_source(project_id: int, url_id: int, source: str, source_detail: str | None) -> None:
    """Record one extra provenance row for a URL that was already stored via upsert_url.

    Used to attach every contributing (backend, engine) pair to a merged search result
    without re-running the full upsert (title/snippet/kind logic already ran once).
    """
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO url_sources(project_id, url_id, source, source_detail, discovered_at) VALUES (?,?,?,?,?)",
            (project_id, url_id, source, source_detail, utcnow()),
        )


def add_capture(
    url_id: int,
    source: str,
    captured_at: str | None,
    status_code: int | None,
    mime: str | None,
    digest: str | None,
    raw: dict,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO captures(url_id, source, captured_at, status_code, mime, digest, raw_json)
            VALUES (?,?,?,?,?,?,?)
            """,
            (url_id, source, captured_at, status_code, mime, digest, json.dumps(raw, ensure_ascii=False)),
        )


def save_page(url_id: int, title: str | None, text: str, content_hash: str) -> None:
    now = utcnow()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO pages(url_id, title, text, content_hash, fetched_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(url_id) DO UPDATE SET
                title=excluded.title,
                text=excluded.text,
                content_hash=excluded.content_hash,
                fetched_at=excluded.fetched_at
            """,
            (url_id, title, text, content_hash, now),
        )
        conn.execute(
            "UPDATE urls SET fetched_at=?, title=COALESCE(?, title) WHERE id=?",
            (now, title, url_id),
        )


def get_url_by_value(url: str) -> dict | None:
    canonical = normalize_url(url)
    with db() as conn:
        row = conn.execute("SELECT * FROM urls WHERE url_hash=?", (url_hash(canonical),)).fetchone()
        return dict(row) if row else None


def list_urls(
    project_id: int,
    *,
    q: str | None = None,
    source: str | None = None,
    domain: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    clauses = ["pu.project_id=?"]
    params: list = [project_id]
    if q:
        clauses.append("(u.url LIKE ? OR u.title LIKE ? OR u.snippet LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if domain:
        clauses.append("u.domain=?")
        params.append(normalize_domain(domain))
    if kind:
        clauses.append("u.kind=?")
        params.append(kind)
    if source:
        clauses.append("EXISTS (SELECT 1 FROM url_sources s2 WHERE s2.project_id=pu.project_id AND s2.url_id=u.id AND s2.source=?)")
        params.append(source)
    params.extend([min(limit, 1000), max(offset, 0)])
    sql = f"""
        SELECT u.*,
               GROUP_CONCAT(DISTINCT us.source) AS sources
        FROM project_urls pu
        JOIN urls u ON u.id=pu.url_id
        LEFT JOIN url_sources us ON us.url_id=u.id AND us.project_id=pu.project_id
        WHERE {' AND '.join(clauses)}
        GROUP BY u.id
        ORDER BY u.last_seen_at DESC
        LIMIT ? OFFSET ?
    """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def search_local(project_id: int, query: str, limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT u.url, u.domain, p.title,
                   snippet(pages_fts, 1, '<mark>', '</mark>', ' … ', 24) AS snippet,
                   bm25(pages_fts) AS rank
            FROM pages_fts
            JOIN pages p ON p.id=pages_fts.rowid
            JOIN urls u ON u.id=p.url_id
            JOIN project_urls pu ON pu.url_id=u.id
            WHERE pages_fts MATCH ? AND pu.project_id=?
            ORDER BY rank
            LIMIT ?
            """,
            (query, project_id, min(limit, 200)),
        ).fetchall()
        return [dict(r) for r in rows]


def get_stats(project_id: int) -> dict:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM project_urls WHERE project_id=?", (project_id,)).fetchone()["n"]
        fetched = conn.execute(
            "SELECT COUNT(*) n FROM project_urls pu JOIN urls u ON u.id=pu.url_id WHERE pu.project_id=? AND u.fetched_at IS NOT NULL",
            (project_id,),
        ).fetchone()["n"]
        domains = conn.execute(
            "SELECT COUNT(DISTINCT u.domain) n FROM project_urls pu JOIN urls u ON u.id=pu.url_id WHERE pu.project_id=?",
            (project_id,),
        ).fetchone()["n"]
        sources = conn.execute(
            "SELECT source, COUNT(DISTINCT url_id) n FROM url_sources WHERE project_id=? GROUP BY source ORDER BY n DESC",
            (project_id,),
        ).fetchall()
        return {"urls": total, "fetched": fetched, "domains": domains, "sources": [dict(r) for r in sources]}
