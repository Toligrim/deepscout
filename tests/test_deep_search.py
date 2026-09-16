import asyncio
import dataclasses

import pytest

from app import db as store
from app.services import deep_search
from app.services.aggregation import AggregatedSearchResponse, MergedResult
from app.services.deep_search import run_deep_search_job, score_domain, select_domains


def run(coro):
    return asyncio.run(coro)


def merged(url, *, rank=1, engines=("brave",), backends=("searxng",)):
    return MergedResult(
        canonical_url=url, title=f"Title for {url}", snippet="a snippet",
        backends=list(backends), engines=list(engines), best_rank=rank, score=1.0,
        contributions=[(b, engines[0] if engines else None) for b in backends],
    )


# --- score_domain / select_domains (pure, no mocking needed) ---------------------


NO_LEXICAL_MATCH_QUERY = "zzqx nonexistent unrelated term"  # keeps domain_query_relevance at 0


def test_score_domain_rewards_best_rank_engine_and_backend_diversity():
    urls = [
        merged("https://a.example/1", rank=1, engines=["brave", "google"], backends=["searxng", "openserp"]),
        merged("https://a.example/2", rank=3, engines=["brave"], backends=["searxng"]),
    ]
    score, explanation = score_domain(urls, NO_LEXICAL_MATCH_QUERY)
    assert explanation["best_rank"] == 1
    assert explanation["url_count"] == 2
    assert explanation["engines"] == ["brave", "google"]
    assert explanation["backends"] == ["openserp", "searxng"]
    assert explanation["domain_query_relevance"] == 0.0
    # 1/1 + 0.1*2 + 0.3*2 + 0.5(backends>1) + 0.3*0(no lexical match) = 2.3
    assert score == pytest.approx(2.3)


def test_score_domain_lexical_bonus_is_small_and_additive():
    urls = [merged("https://a.example/1", rank=5, engines=["brave"], backends=["searxng"])]  # weak SERP signal
    no_match_score, _ = score_domain(urls, NO_LEXICAL_MATCH_QUERY)
    match_score, explanation = score_domain(urls, "title")  # merged()'s title is "Title for <url>"
    assert explanation["domain_query_relevance"] > 0
    # the bonus nudges the score up but stays small relative to the SERP-signal terms
    assert match_score > no_match_score
    assert (match_score - no_match_score) <= 0.3 + 1e-9


def test_select_domains_respects_max_domains_and_is_deterministic():
    results = [
        merged("https://a.example/1", rank=1),
        merged("https://b.example/1", rank=2),
        merged("https://c.example/1", rank=3),
        merged("https://d.example/1", rank=4),
    ]
    top2 = select_domains(results, max_domains=2, query=NO_LEXICAL_MATCH_QUERY)
    assert len(top2) == 2
    assert [d for d, *_ in top2] == ["a.example", "b.example"]
    # re-running must give the exact same order (pure function, deterministic tie-break)
    assert select_domains(results, max_domains=2, query=NO_LEXICAL_MATCH_QUERY) == top2


def test_select_domains_tie_break_is_domain_name():
    results = [merged("https://z.example/1", rank=1), merged("https://a.example/1", rank=1)]
    ordered = select_domains(results, max_domains=2, query=NO_LEXICAL_MATCH_QUERY)
    assert [d for d, *_ in ordered] == ["a.example", "z.example"]


# --- run_deep_search_job (integration, everything mocked) ------------------------


def _fake_run_search(results, backends=None, warnings=None):
    async def _run(query, **kwargs):
        return AggregatedSearchResponse(query=query, results=results, backends=backends or {}, warnings=warnings or [])

    return _run


def _fake_discovery(items=None, error=None):
    async def _call(domain, limit):
        if error:
            raise RuntimeError(error)
        return items or []

    return _call


DEFAULT_PARAMS = {"max_serp_results": 50, "max_domains": 10, "limit_per_source": 100, "sources": ["sitemap", "wayback", "commoncrawl"]}


def test_serp_results_feed_into_pipeline_and_are_persisted(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a", rank=1)]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    fetched = store.get_job(job["id"])
    assert fetched["status"] == "completed"
    rows = store.list_job_urls(job["id"], tiers=None)
    assert any(r["url"] == "https://example.com/a" for r in rows)


def test_discovery_limit_per_source_is_passed_through(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    seen_limits = []

    async def capture(domain, limit):
        seen_limits.append(limit)
        return []

    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", capture)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", capture)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", capture)

    params = {**DEFAULT_PARAMS, "limit_per_source": 42}
    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", params))

    assert seen_limits and all(limit == 42 for limit in seen_limits)


def test_max_domains_bounds_number_of_domains_discovered(temp_db, monkeypatch):
    results = [merged(f"https://d{i}.example/1", rank=i + 1) for i in range(5)]
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search(results))
    called_domains = set()

    async def capture(domain, limit):
        called_domains.add(domain)
        return []

    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", capture)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", capture)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", capture)

    params = {**DEFAULT_PARAMS, "max_domains": 2}
    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", params))

    assert called_domains == {"d0.example", "d1.example"}


def test_discovery_concurrency_is_bounded(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "settings", dataclasses.replace(deep_search.settings, deep_search_concurrency=2))
    results = [merged(f"https://d{i}.example/1", rank=i + 1) for i in range(4)]
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search(results))

    current = {"n": 0, "max": 0}

    async def slow(domain, limit):
        current["n"] += 1
        current["max"] = max(current["max"], current["n"])
        await asyncio.sleep(0.02)
        current["n"] -= 1
        return []

    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", slow)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", slow)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", slow)

    params = {**DEFAULT_PARAMS, "max_domains": 4}  # 4 domains x 3 sources = 12 calls
    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", params))

    assert current["max"] <= 2


def test_concurrency_is_shared_globally_across_two_simultaneous_jobs(temp_db, monkeypatch):
    """The bug this guards against: a Semaphore created fresh inside each
    run_deep_search_job() call would let N concurrently-running jobs each get their own
    `deep_search_concurrency` budget, N times the intended total. Two jobs running at
    once must share ONE budget."""
    monkeypatch.setattr(deep_search, "settings", dataclasses.replace(deep_search.settings, deep_search_concurrency=2))
    deep_search._global_semaphore = None
    deep_search._global_semaphore_loop = None

    async def fake_run_search(query, **kwargs):
        n = 3
        prefix = "a" if query == "job-a" else "b"
        results = [merged(f"https://{prefix}{i}.example/1", rank=i + 1) for i in range(n)]
        return AggregatedSearchResponse(query=query, results=results, backends={}, warnings=[])

    monkeypatch.setattr(deep_search, "run_search", fake_run_search)

    current = {"n": 0, "max": 0}

    async def slow(domain, limit):
        current["n"] += 1
        current["max"] = max(current["max"], current["n"])
        await asyncio.sleep(0.02)
        current["n"] -= 1
        return []

    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", slow)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", slow)
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", slow)

    params = {**DEFAULT_PARAMS, "max_domains": 3}  # 3 domains x 3 sources = 9 calls per job
    job_a = store.create_job(1, "deep_search", {})
    job_b = store.create_job(1, "deep_search", {})

    async def main():
        await asyncio.gather(
            run_deep_search_job(job_a["id"], 1, "job-a", params),
            run_deep_search_job(job_b["id"], 1, "job-b", params),
        )

    run(main())

    assert store.get_job(job_a["id"])["status"] == "completed"
    assert store.get_job(job_b["id"])["status"] == "completed"
    assert current["max"] <= 2  # observed across BOTH jobs, not per job


def test_sitemap_failure_does_not_break_job(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery(error="boom"))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([{"url": "https://example.com/w"}]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    fetched = store.get_job(job["id"])
    assert fetched["status"] == "partial"
    rows = store.list_job_urls(job["id"], tiers=None)
    assert any(r["url"] == "https://example.com/w" for r in rows)


def test_wayback_failure_does_not_break_job(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([{"url": "https://example.com/s"}]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery(error="boom"))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    assert store.get_job(job["id"])["status"] == "partial"


def test_commoncrawl_failure_does_not_break_job(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery(error="boom"))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    assert store.get_job(job["id"])["status"] == "partial"


def test_all_discovery_failed_but_serp_present_is_partial_not_failed(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery(error="down"))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery(error="down"))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery(error="down"))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    fetched = store.get_job(job["id"])
    assert fetched["status"] == "partial"  # SERP's one URL is still a usable result
    rows = store.list_job_urls(job["id"], tiers=None)
    assert len(rows) == 1


def test_zero_usable_results_is_failed(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    fetched = store.get_job(job["id"])
    assert fetched["status"] == "failed"


def test_completed_status_when_nothing_reported_a_problem(temp_db, monkeypatch):
    monkeypatch.setattr(deep_search, "run_search", _fake_run_search([merged("https://example.com/a")]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    assert store.get_job(job["id"])["status"] == "completed"


def test_url_found_via_four_sources_is_stored_once_with_full_provenance(temp_db, monkeypatch):
    monkeypatch.setattr(
        deep_search, "run_search",
        _fake_run_search([merged("https://example.com/shared", engines=["brave"], backends=["searxng"])]),
    )
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "sitemap", _fake_discovery([{"url": "https://example.com/shared"}]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([{"url": "https://example.com/shared"}]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([{"url": "https://example.com/shared"}]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "example", DEFAULT_PARAMS))

    rows = store.list_job_urls(job["id"], tiers=None)
    matches = [r for r in rows if r["url"] == "https://example.com/shared"]
    assert len(matches) == 1  # stored once, not 4 times
    assert {p["source"] for p in matches[0]["provenance"]} == {"searxng", "sitemap", "wayback", "commoncrawl"}
    assert matches[0]["source_count"] == 4  # 4 distinct source types, each counted once


def test_relevance_tiers_and_progress_counters_are_recorded(temp_db, monkeypatch):
    monkeypatch.setattr(
        deep_search, "run_search",
        _fake_run_search([merged("https://example.com/bgp-routing-guide")]),
    )
    monkeypatch.setitem(
        deep_search._SOURCE_CALLS, "sitemap",
        _fake_discovery([{"url": "https://example.com/unrelated-page"}]),
    )
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "wayback", _fake_discovery([]))
    monkeypatch.setitem(deep_search._SOURCE_CALLS, "commoncrawl", _fake_discovery([]))

    job = store.create_job(1, "deep_search", {})
    run(run_deep_search_job(job["id"], 1, "bgp routing", DEFAULT_PARAMS))

    fetched = store.get_job(job["id"])
    import json

    progress = json.loads(fetched["progress_json"])
    assert progress["counters"]["unique_urls"] == 2
    assert progress["counters"]["high_relevance"] >= 1  # the SERP URL matches "bgp"/"routing"
    assert progress["counters"]["discovered_relevance"] >= 1  # the unrelated sitemap page

    rows = store.list_job_urls(job["id"], tiers=None)
    tiers = {r["url"]: r["relevance_tier"] for r in rows}
    assert tiers["https://example.com/bgp-routing-guide"] == "high"
    assert tiers["https://example.com/unrelated-page"] == "discovered"
