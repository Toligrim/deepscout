# DeepScout

DeepScout is a self-hosted manual deep-search workbench. It combines normal web search with URL discovery from sitemaps, the Internet Archive Wayback CDX index, Common Crawl, live crawling, and a local SQLite/FTS5 research library.

The goal is not to replace a search engine. The goal is to make poorly indexed pages easier to discover and investigate by hand.

## MVP features

- Search through your own SearXNG instance and save every result into a project.
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
git clone <your-repo-url>
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
- `POST /api/discover/domain`
- `POST /api/fetch`
- `GET /api/urls`
- `GET /api/local-search`
- `GET /api/stats`

Interactive OpenAPI docs are available at `/docs`.

## Politeness and limits

DeepScout is intended for research, not aggressive crawling. Live crawl obeys robots.txt when it can be retrieved, stays on the selected domain, and is deliberately capped. Wayback and Common Crawl are queried through public indexes; keep per-source limits reasonable.

## Current limitations

- Discovery jobs currently run in the request process instead of a persistent job queue.
- Wayback queries use a single bounded request; very large domains will later need resume-key pagination.
- Common Crawl uses the latest collection only.
- JS-only pages are not rendered yet; a Playwright fallback is planned.
- Search currently uses SearXNG only; OpenSERP can be added as a second provider.
- PDF text extraction and WARC body retrieval are not in the MVP yet.

## Roadmap

1. Streaming discovery progress with SSE.
2. Wayback resume-key pagination and multiple Common Crawl collections.
3. PDF text extraction and document preview.
4. Playwright fallback for JavaScript-heavy pages.
5. Query Builder for site/file/date/phrase operators.
6. Provenance graph: why a URL was discovered and from which parent/query.
7. Export to CSV/JSONL/Markdown.
8. Optional MCP server for Hermes/Codex and optional LLM query expansion.

## License

MIT
