import json
import sqlite3

from app import db as store


def test_create_and_get_job(temp_db):
    job = store.create_job(1, "deep_search", {"query": "bgp routing"})
    assert job["status"] == "queued"
    assert job["kind"] == "deep_search"
    assert json.loads(job["params_json"]) == {"query": "bgp routing"}

    fetched = store.get_job(job["id"])
    assert fetched["id"] == job["id"]
    assert fetched["status"] == "queued"


def test_get_job_missing_returns_none(temp_db):
    assert store.get_job(999999) is None


def test_update_job_sets_fields(temp_db):
    job = store.create_job(1, "deep_search", {})
    store.update_job(job["id"], status="running", started_at="2026-01-01T00:00:00")
    fetched = store.get_job(job["id"])
    assert fetched["status"] == "running"
    assert fetched["started_at"] == "2026-01-01T00:00:00"


def test_update_job_rejects_unknown_field(temp_db):
    job = store.create_job(1, "deep_search", {})
    try:
        store.update_job(job["id"], not_a_real_column="x")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_update_job_noop_on_empty_kwargs(temp_db):
    job = store.create_job(1, "deep_search", {})
    store.update_job(job["id"])  # must not raise
    assert store.get_job(job["id"])["status"] == "queued"


def test_mark_interrupted_jobs_flips_queued_and_running_only(temp_db):
    queued = store.create_job(1, "deep_search", {})
    running = store.create_job(1, "deep_search", {})
    store.update_job(running["id"], status="running")
    completed = store.create_job(1, "deep_search", {})
    store.update_job(completed["id"], status="completed")

    changed = store.mark_interrupted_jobs()

    assert changed == 2
    assert store.get_job(queued["id"])["status"] == "interrupted"
    assert store.get_job(running["id"])["status"] == "interrupted"
    assert store.get_job(completed["id"])["status"] == "completed"  # untouched


def test_add_job_url_and_list_job_urls_with_tier_filter(temp_db):
    job = store.create_job(1, "deep_search", {})
    row = store.upsert_url(1, "https://example.com/bgp", source="sitemap", source_detail=None, title="BGP guide")
    store.add_job_url(job["id"], row["id"], 0.9, "high", 1.5, 3, {"note": "test"})

    row2 = store.upsert_url(1, "https://example.com/about", source="sitemap", source_detail=None, title="About")
    store.add_job_url(job["id"], row2["id"], 0.0, "discovered", 1.5, None, {})

    all_rows = store.list_job_urls(job["id"], tiers=None)
    assert len(all_rows) == 2

    high_only = store.list_job_urls(job["id"], tiers=["high"])
    assert len(high_only) == 1
    assert high_only[0]["url"] == "https://example.com/bgp"
    assert high_only[0]["relevance_score"] == 0.9
    assert high_only[0]["domain_score"] == 1.5
    assert high_only[0]["best_serp_rank"] == 3


def _provenance_pairs(row):
    return {(p["source"], p["detail"]) for p in row["provenance"]}


def test_list_job_urls_reports_this_jobs_own_sources(temp_db):
    job = store.create_job(1, "deep_search", {})
    row = store.upsert_url(1, "https://example.com/a", source="searxng", source_detail="brave")
    store.add_job_url_source(job["id"], row["id"], "searxng", "brave")
    store.add_job_url_source(job["id"], row["id"], "openserp", "bing")
    store.add_job_url_source(job["id"], row["id"], "wayback", None)
    store.add_job_url(job["id"], row["id"], 1.0, "high", 2.0, 1, {})

    rows = store.list_job_urls(job["id"])
    assert _provenance_pairs(rows[0]) == {("searxng", "brave"), ("openserp", "bing"), ("wayback", None)}
    assert rows[0]["source_count"] == 3


def test_job_specific_provenance_does_not_leak_between_jobs(temp_db):
    """The regression the task asked for: Job A finds a URL via Wayback, Job B later
    finds the same URL only via OpenSERP/Bing — Job B's results must show only
    OpenSERP/Bing, even though the shared url_sources row now has both."""
    row = store.upsert_url(1, "https://example.com/shared", source="wayback", source_detail=None)

    job_a = store.create_job(1, "deep_search", {})
    store.add_job_url_source(job_a["id"], row["id"], "wayback", None)
    store.add_job_url(job_a["id"], row["id"], 0.5, "possible", 1.0, None, {})

    # a second, unrelated project search re-finds the same URL through OpenSERP/Bing,
    # which also lands in the shared, project-wide url_sources table
    store.add_url_source(1, row["id"], "openserp", "bing")

    job_b = store.create_job(1, "deep_search", {})
    store.add_job_url_source(job_b["id"], row["id"], "openserp", "bing")
    store.add_job_url(job_b["id"], row["id"], 0.9, "high", 1.0, 1, {})

    job_a_rows = store.list_job_urls(job_a["id"])
    job_b_rows = store.list_job_urls(job_b["id"])

    assert _provenance_pairs(job_a_rows[0]) == {("wayback", None)}
    assert _provenance_pairs(job_b_rows[0]) == {("openserp", "bing")}  # not wayback too

    # the project-wide provenance still legitimately has both
    conn = sqlite3.connect(store.settings.db_path)
    global_sources = {r[0] for r in conn.execute("SELECT source FROM url_sources WHERE url_id=?", (row["id"],))}
    conn.close()
    assert global_sources == {"wayback", "openserp"}


def test_provenance_preserves_all_four_source_engine_pairs(temp_db):
    """The task's exact scenario: one URL found via OpenSERP/Bing, OpenSERP/DuckDuckGo,
    SearXNG/Brave, and Wayback — the API result must carry all four pairs, not just an
    aggregated source list."""
    job = store.create_job(1, "deep_search", {})
    row = store.upsert_url(1, "https://example.com/multi", source="searxng", source_detail="brave")
    store.add_job_url_source(job["id"], row["id"], "openserp", "bing")
    store.add_job_url_source(job["id"], row["id"], "openserp", "duckduckgo")
    store.add_job_url_source(job["id"], row["id"], "searxng", "brave")
    store.add_job_url_source(job["id"], row["id"], "wayback", None)
    store.add_job_url(job["id"], row["id"], 1.0, "high", 1.0, 1, {})

    rows = store.list_job_urls(job["id"])
    assert _provenance_pairs(rows[0]) == {
        ("openserp", "bing"), ("openserp", "duckduckgo"), ("searxng", "brave"), ("wayback", None),
    }


def test_independent_source_count_caps_discovery_collections_at_one(temp_db):
    """Two Common Crawl collections for the same URL are one independent signal, not
    two — but two different search engines still count separately. Expected total: 3
    (Common Crawl once, Bing, DuckDuckGo), not 4."""
    job = store.create_job(1, "deep_search", {})
    row = store.upsert_url(1, "https://example.com/cc", source="commoncrawl", source_detail="CC-MAIN-2026-34")
    store.add_job_url_source(job["id"], row["id"], "commoncrawl", "CC-MAIN-2026-34")
    store.add_job_url_source(job["id"], row["id"], "commoncrawl", "CC-MAIN-2026-30")
    store.add_job_url_source(job["id"], row["id"], "openserp", "bing")
    store.add_job_url_source(job["id"], row["id"], "openserp", "duckduckgo")
    store.add_job_url(job["id"], row["id"], 1.0, "high", 1.0, None, {})

    rows = store.list_job_urls(job["id"])
    assert rows[0]["source_count"] == 3
    # detail is still preserved for both collections, just not double-counted
    cc_details = {p["detail"] for p in rows[0]["provenance"] if p["source"] == "commoncrawl"}
    assert cc_details == {"CC-MAIN-2026-34", "CC-MAIN-2026-30"}


def test_list_job_urls_sort_order_source_count_then_rank_then_score_then_url(temp_db):
    """source_count DESC -> best_serp_rank ASC (NULLs last) -> relevance_score DESC -> url ASC,
    all within the same tier."""
    job = store.create_job(1, "deep_search", {})

    def add(url, *, sources, rank, score):
        row = store.upsert_url(1, url, source="sitemap", source_detail=None)
        for i, src in enumerate(sources):
            store.add_job_url_source(job["id"], row["id"], src, f"detail{i}")
        store.add_job_url(job["id"], row["id"], score, "high", None, rank, {})
        return row

    # z.example: 2 sources, rank 5 -> loses to y.example (2 sources, rank 3) on rank
    add("https://z.example/", sources=["sitemap", "wayback"], rank=5, score=0.9)
    add("https://y.example/", sources=["sitemap", "wayback"], rank=3, score=0.7)
    # x.example: only 1 source -> ranks below both, even though its score is highest
    add("https://x.example/", sources=["sitemap"], rank=1, score=0.99)
    # w.example: 2 sources, no SERP rank at all (discovery-only) -> NULL rank sorts last
    add("https://w.example/", sources=["sitemap", "commoncrawl"], rank=None, score=0.95)
    # v.example: same source_count and rank as y.example, lower score -> loses on score
    add("https://v.example/", sources=["sitemap", "wayback"], rank=3, score=0.6)

    rows = store.list_job_urls(job["id"], tiers=["high"])
    assert [r["url"] for r in rows] == [
        "https://y.example/",  # 2 sources, rank 3, score 0.7
        "https://v.example/",  # 2 sources, rank 3, score 0.6 (loses to y on score)
        "https://z.example/",  # 2 sources, rank 5 (a real rank still beats NULL)
        "https://w.example/",  # 2 sources, rank NULL -- sorts after any real rank
        "https://x.example/",  # 1 source only -- last regardless of its high score
    ]


def test_jobs_migration_compatible_with_pre_existing_old_schema_db(tmp_path, monkeypatch):
    """Simulate a production DB from before jobs/job_urls existed: init_db() must add
    the new tables idempotently without touching or losing any existing data."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO projects(name, created_at) VALUES ('Default', '2025-01-01T00:00:00');
        CREATE TABLE urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url_hash TEXT NOT NULL UNIQUE, url TEXT NOT NULL UNIQUE,
            domain TEXT NOT NULL, title TEXT, snippet TEXT, kind TEXT NOT NULL DEFAULT 'page', mime TEXT,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, live_status INTEGER, fetched_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    import dataclasses

    isolated = dataclasses.replace(store.settings, db_path=db_path)
    monkeypatch.setattr(store, "settings", isolated)

    store.init_db()  # must not raise, must not touch existing rows

    projects = store.list_projects()
    assert len(projects) == 1
    assert projects[0]["name"] == "Default"

    # new tables exist and are usable
    job = store.create_job(projects[0]["id"], "deep_search", {})
    assert store.get_job(job["id"])["status"] == "queued"
