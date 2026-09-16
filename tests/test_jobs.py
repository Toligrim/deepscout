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
    store.add_job_url(job["id"], row["id"], 0.9, "high", 1.5, {"note": "test"})

    row2 = store.upsert_url(1, "https://example.com/about", source="sitemap", source_detail=None, title="About")
    store.add_job_url(job["id"], row2["id"], 0.0, "discovered", 1.5, {})

    all_rows = store.list_job_urls(job["id"], 1, tiers=None)
    assert len(all_rows) == 2

    high_only = store.list_job_urls(job["id"], 1, tiers=["high"])
    assert len(high_only) == 1
    assert high_only[0]["url"] == "https://example.com/bgp"
    assert high_only[0]["relevance_score"] == 0.9
    assert high_only[0]["domain_score"] == 1.5


def test_list_job_urls_includes_provenance_backends(temp_db):
    job = store.create_job(1, "deep_search", {})
    row = store.upsert_url(1, "https://example.com/a", source="searxng", source_detail="brave")
    store.add_url_source(1, row["id"], "openserp", "bing")
    store.add_url_source(1, row["id"], "wayback", None)
    store.add_job_url(job["id"], row["id"], 1.0, "high", 2.0, {})

    rows = store.list_job_urls(job["id"], 1)
    assert set(rows[0]["backends"].split(",")) == {"searxng", "openserp", "wayback"}


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
