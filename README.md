# DeepScout

DeepScout is a self-hosted manual deep-search workbench. It combines normal web search with URL discovery from sitemaps, the Internet Archive Wayback CDX index, Common Crawl, live crawling, and a local SQLite/FTS5 research library.

The goal is not to replace a search engine. The goal is to make poorly indexed pages easier to discover and investigate by hand.

## MVP features

- Multi-backend search — SearXNG and [OpenSERP](https://github.com/karust/openserp) run in
  parallel, are deduplicated and ranked deterministically, and every result keeps full
  backend/engine provenance. One backend failing (or an engine hitting CAPTCHA/429) never fails
  the whole request — see [Search backends](#search-backends) below.
- Deep Search — a deterministic background job that takes a Search, picks the most promising
  domains out of the results, and expands each one through Sitemap/Wayback/Common Crawl, scored
  against your query with cheap lexical matching and merged into one deduplicated result set —
  see [Deep Search](#deep-search) below.
- Domain Explorer with independent discovery providers:
  - sitemap.xml and sitemap indexes;
  - robots.txt Sitemap declarations;
  - Wayback Machine CDX index;
  - latest Common Crawl URL index;
  - optional same-domain live crawl.
- Canonical URL store that merges the same URL discovered by multiple sources.
- URL type classification: page, PDF, office document, archive, image.
- Fetch a page, extract readable text and outgoing links, and add discovered links to the project.
- SQLite WAL database and FTS5 full-text search over fetched pages.
- Mobile-first single-page UI that works well from an iPhone.
- No LLM required.

## Architecture

Browser UI → FastAPI → search/discovery adapters → canonical URL store → SQLite + FTS5.

The URL is the central entity. Search results, sitemaps, archive captures and links all enrich the same canonical URL instead of creating separate copies.

## Quick start on Raspberry Pi / Ubuntu

Requires Python 3.12+ and a SearXNG instance with JSON output enabled.

```bash
git clone https://github.com/Toligrim/deepscout.git
cd deepscout
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
set -a; source .env; set +a
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open `http://<raspberry-pi-ip>:8080`.

Your existing SearXNG can stay on `127.0.0.1:8888`; set:

```bash
SEARXNG_URL=http://127.0.0.1:8888
```

SearXNG must allow `format=json` in its `search.formats` configuration.

Optionally run [OpenSERP](https://github.com/karust/openserp) as a second, independent search
backend and point DeepScout at it:

```bash
OPENSERP_URL=http://127.0.0.1:7000
```

DeepScout works fine with only one of the two backends configured/healthy — see below.

## Docker

```bash
cp .env.example .env
docker compose up -d --build
```

If SearXNG is running on the host at port 8888, the supplied compose file uses `host.docker.internal`.

## Usage

### Search

Use normal queries and search-engine operators such as:

- `"rare phrase"`
- `site:example.com bgp`
- `filetype:pdf anycast`

Each result is automatically stored in the selected project.

### Search backends

DeepScout answers a normal Search query from two independent backends, run in parallel:

- **SearXNG** — whichever engines are enabled on your SearXNG instance.
- **OpenSERP** — a self-hosted browser-based SERP tool that can reach Google, Bing, Yandex,
  DuckDuckGo, Ecosia (Baidu is opt-in, off by default).

By default DeepScout queries every configured backend directly, in parallel, on every request —
there's no health precheck first (that would just add a round-trip; a failing/degraded backend is
already isolated from the others once the real request is in flight). The UI lets you restrict a
search to just one backend, and pick which OpenSERP engines to query. Results are deduplicated by
canonical URL (the same normalization already used everywhere else in DeepScout — tracking
params stripped, trailing slash/host case normalized) and merged: a URL found through both
backends keeps a card for *all* of its sources (backend + engine), never duplicate cards. OpenSERP
itself already clusters a URL across its own engines (`/mega/search` with `dedupe=false&merge=true`,
parsed via its `clusters` field) — DeepScout only needs to merge SearXNG's results into that.

Each backend's per-request status is one of:

- `ok` — no known engine failures.
- `degraded` — usable results came back, but at least one engine hit a CAPTCHA/403/429/timeout.
- `failed` — the backend gave no usable result at all (network/HTTP error, or every engine failed).

`language`/`page`/`time_range` are translated into OpenSERP's real query parameters (`lang`,
`start`+`limit`, and `date=YYYYMMDD..YYYYMMDD` respectively — confirmed against its OpenAPI spec),
not just passed through SearXNG's own parameter names.

Ranking is a small, deterministic, unit-tested formula (`app/services/aggregation.py`):

```
score = 1 / best_rank_across_contributors
      + 0.3 * (distinct_engine_count - 1)
      + 0.5 * (found in more than one backend)
      + 0.1 * SearXNG's own relevance score, if present
```

**DeepScout never tries to solve CAPTCHAs, rotate proxies, or otherwise evade a search engine's
anti-bot protection.** When an engine returns a CAPTCHA/403/429/timeout, DeepScout reports it as
`degraded` and keeps working with whatever else responded — it does not retry harder or spoof a
different fingerprint to get around the block.

`GET /api/search/health` (cached in memory for `DEEPSCOUT_HEALTH_CACHE_SECONDS`, default 30s)
reports, per backend and per engine, one of `ok` / `degraded` / `failed` / `unknown` plus a short
reason (`CAPTCHA`, `blocked`, `rate_limited`, `timeout`, …) — never a raw stack trace. Wayback and
Common Crawl reachability are reported separately, under `discovery`, since they aren't SERP
engines.

### Deep Search

Normal Search answers "what do search engines think is relevant right now?" Deep Search answers
"what else exists around this topic that a plain search wouldn't surface?" — it's the same query,
expanded automatically into the domains it points at, using the archival/structural sources
Domain Explorer already offers, one level deep. It's a fully deterministic pipeline — no LLM, no
embeddings, nothing that "decides" beyond the rules documented here.

**Pipeline**, all against the existing app code, nothing reimplemented:

1. **SERP** — the same `run_search()` multi-backend Search uses, top `max_serp_results` kept
   (default 50, max 200).
2. **Domain selection** — group the kept SERP results by domain, score each one, keep the top
   `max_domains` (default 10, max 25):

   ```
   domain_score = 1/best_rank_of_any_url_in_domain
                + 0.1 * min(url_count_in_serp, 5)
                + 0.3 * distinct_engine_count
                + 0.5 * (found through more than one backend)
   ```

   Sorted descending, ties broken by domain name. Every selected domain's score and its inputs
   (best rank, URL count, engines, backends) are saved in the job's result summary, so you can
   see *why* a domain made the cut.
3. **Discovery** — for each selected domain, the existing `discover_sitemaps` / `discover_wayback`
   / `discover_commoncrawl` (same functions and limits Domain Explorer uses), one `(domain,
   source)` call at a time bounded by an internal concurrency limit (`DEEPSCOUT_DEEP_SEARCH_CONCURRENCY`,
   default 4) so a big Deep Search doesn't hammer Wayback/Common Crawl or the Pi itself. Each
   source failing for a domain is isolated to that one `(domain, source)` pair — it never aborts
   the others. **Live Crawl is not offered here** — it stays a Domain Explorer-only, explicitly
   opt-in action; automatically crawling sites as a side effect of a Deep Search wasn't something
   this feature should do without you asking for that domain specifically.
4. **Relevance** — every URL touched by the job (SERP and discovered alike) gets scored against
   the query with a cheap lexical matcher, never a fetch: tokens are lowercased and split on
   anything non-alphanumeric (Unicode-aware, so this works the same for Cyrillic queries), then
   for each query token the best match location decides its weight — the URL/path/filename itself
   (1.0), the title (0.6), or the snippet (0.3) — averaged into a 0..1 score.
   `score >= 0.6` → **high**, `0 < score < 0.6` → **possible**, `score == 0` → **discovered**. A
   URL is *never* dropped for scoring 0 — that's the whole point of a discovery tool, it just
   sorts last.
5. **Merge** — one row per URL in a new `job_urls` table (relevance score/tier, and the score of
   the domain it came from) linking to the same `urls`/`url_sources` rows everything else in
   DeepScout already uses — no separate URL store, and a URL found by e.g. both an engine and
   Wayback keeps every source's provenance.

**Sort order** shown to you: tier first (high → possible → discovered), then within a tier: found
through more independent sources, then better SERP rank, then the lexical score itself, then the
URL string as a final deterministic tie-breaker.

**Job status**: `failed` only if the whole pipeline produced zero usable results (or an actual
internal bug aborted it); `partial` if there's at least one usable result but something also
logged a problem (a degraded SERP backend, a failed discovery call); `completed` only if nothing
anywhere reported an issue. A single domain's Common Crawl call timing out does not fail your
Deep Search — you still get everything that did work, with the failure listed separately.

**API**: `POST /api/deep-search` returns `{"job_id": ...}` immediately; the pipeline runs as a
background `asyncio` task (SQLite + asyncio only — no Redis/Celery, appropriate for a
single-Raspberry-Pi deployment). `GET /api/deep-search/{id}` returns a JSON snapshot (status,
progress, result summary). `GET /api/deep-search/{id}/results?tiers=high,possible` (or
`tiers=all`) lists the found URLs. `GET /api/deep-search/{id}/events` streams progress over
**SSE** — a real `text/event-stream`, but its data source is simply polling the same `jobs` row
once a second rather than a purpose-built pub/sub broadcaster: the database row stays the single
source of truth, more than one browser tab watching the same job works for free, and a server
restart mid-job is trivially recoverable (the client just reconnects and immediately sees whatever
the row currently says — nothing to replay). If the server does restart mid-job, that job is
marked `interrupted` on the next startup rather than left stuck as `running` forever.

### Domain Explorer

Enter a domain and select discovery providers. DeepScout runs them independently; one provider can fail without aborting the others. Results can be filtered by substring, source, and document type.

### Fetch + link expansion

Click `Текст` on a URL. DeepScout downloads HTML, extracts text, stores it for FTS5 search and adds outgoing links to the URL frontier.

### Local search

The `База` tab searches only content that you explicitly fetched. This is useful after you have collected material that is difficult to rediscover through normal search engines.

## API

- `GET /api/health`
- `GET /api/projects`
- `POST /api/projects`
- `POST /api/search`
- `GET /api/search/health`
- `POST /api/deep-search`
- `GET /api/deep-search/{job_id}`
- `GET /api/deep-search/{job_id}/results`
- `GET /api/deep-search/{job_id}/events` (SSE)
- `POST /api/discover/domain`
- `POST /api/fetch`
- `GET /api/urls`
- `GET /api/local-search`
- `GET /api/stats`

Interactive OpenAPI docs are available at `/docs`.

## Politeness and limits

DeepScout is intended for research, not aggressive crawling. Live crawl obeys robots.txt when it can be retrieved, stays on the selected domain, and is deliberately capped. Wayback and Common Crawl are queried through public indexes; keep per-source limits reasonable. Deep Search additionally bounds how many `(domain, source)` discovery calls run at once (`DEEPSCOUT_DEEP_SEARCH_CONCURRENCY`, default 4) so a large run doesn't hammer Wayback/Common Crawl or the sites being discovered.

## Current limitations

- Domain Explorer's discovery still runs synchronously in the request (no progress/job model);
  Deep Search has both, Domain Explorer doesn't need them at its smaller single-domain scale yet.
- Wayback queries use a single bounded request; very large domains will later need resume-key pagination.
- Common Crawl uses the latest collection only.
- JS-only pages are not rendered yet; a Playwright fallback is planned.
- PDF text extraction and WARC body retrieval are not in the MVP yet.
- OpenSERP's own multi-engine query (`/mega/search`) can take 15-20s end to end when several
  engines are slow/CAPTCHA'd, since DeepScout only reports what actually happened — it doesn't
  cut a slow engine short. With both backends enabled by default, a normal Search can occasionally
  take noticeably longer than SearXNG alone did in v0.1. Deep Search inherits the same tradeoff
  for its own SERP phase.
- Deep Search's domain scoring only knows what the SERP told it — a generic/ambiguous query can
  occasionally pull in an unrelated high-traffic domain alongside the relevant ones (observed
  during testing). It's not filtered out post-hoc; the relevance scoring on its actual pages
  downgrades it instead of hiding it.

## Roadmap

1. Streaming progress for Domain Explorer too (Deep Search already has it).
2. Wayback resume-key pagination and multiple Common Crawl collections.
3. PDF text extraction and document preview.
4. Playwright fallback for JavaScript-heavy pages.
5. Query Builder for site/file/date/phrase operators.
6. Provenance graph: why a URL was discovered and from which parent/query.
7. Export to CSV/JSONL/Markdown.
8. Optional MCP server for Hermes/Codex and optional LLM query expansion.

## License

MIT
