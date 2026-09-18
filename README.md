# Research Engine Academic Journal Plugin

`marginalia-ai-plugin-academic-journal` adds scholarly literature to a
[Research Engine](https://github.com/John-Cusack/MarginaliaAI) corpus. It discovers papers,
resolves legal open-access copies, downloads and ingests them, extracts their bibliographies,
and records `cites` edges between ingested papers — snowballing through references to a
bounded depth.

It contributes:

| Kind | Id |
|---|---|
| MCP tools | `academic-journal.discover_papers`, `.discover_by_doi`, `.discover_by_author`, `.search_papers`, `.import_manual_pdf`, `.pipeline_status`, `.retry_failed`, `.start_workers`, `.stop_workers` |
| Filter extension | `academic_paper` — year range, venue, citation counts, open access |
| Source-search provider | `acad` — OpenAlex results for core's `search_sources` |
| Extraction schema | `bibliography_references` v1 |
| Post-ingestion hook | `academic-journal.citation_hook` |
| Database | plugin-owned `acad_*` tables, revision 2 |

## Providers

| Provider | Used for | Configuration |
|---|---|---|
| OpenAlex | search, DOI and author lookup, source search | `OPENALEX_EMAIL` (optional, polite pool) |
| Semantic Scholar | search | `SEMANTIC_SCHOLAR_API_KEY` (searched by default only when set) |
| Crossref | search | none |
| Unpaywall | open-access resolution and PDF location | `UNPAYWALL_EMAIL` (Unpaywall asks for a real address) |
| arXiv | direct PDF URLs | none |
| NCBI E-utilities / PubMed Central | PMC copies of PubMed papers | `NCBI_API_KEY` (optional) |
| CORE | open-access resolution | `CORE_API_KEY` (skipped when unset) |

Metadata calls go only to the hosts in `plugin.yaml`'s `network_allowlist`; the plugin refuses
any other API host. Open-access PDFs are then downloaded from wherever the metadata points —
publisher sites and institutional repositories — which is why the manifest declares
`network: full`.

Every provider is rate-limited per process with a token bucket, and a circuit breaker stops
calling a provider after five consecutive failures for 60 seconds:

| Source | Requests/second (burst) |
|---|---|
| OpenAlex | 9 (9) |
| Semantic Scholar | 0.2 (1); 10 (10) with an API key |
| Crossref | 45 (45) |
| Unpaywall | 10 (10) |
| arXiv | 1 (3) |
| NCBI | 3 (3); 9 (9) with an API key |
| CORE | 1 (1) |
| PDF downloads | 2 (2) |

HTTP 429 responses are retried with the server's `Retry-After` (capped at 60 seconds).

## Install

The plugin depends only on `marginalia-ai-sdk`; install it into the same environment as
`marginalia-ai` 0.6.x.

```bash
python -m pip install marginalia-ai marginalia-ai-plugin-academic-journal
```

With pipx:

```bash
pipx install marginalia-ai
pipx inject marginalia-ai marginalia-ai-plugin-academic-journal
```

Installation only makes the static manifest discoverable. Core imports nothing until you
approve the exact distribution:

```bash
research-engine plugin list                          # academic-journal: available
research-engine plugin audit academic-journal        # contributions, permissions, hash, database
research-engine plugin enable academic-journal
research-engine plugin migrate academic-journal      # creates/upgrades the acad_* tables
```

Restart the MCP server afterwards. Core refuses to load the plugin until `migrate` has
recorded revision 2. Upgrading the package changes its version and manifest hash, so it
needs `research-engine plugin approve-upgrade academic-journal` (and `migrate` again when a
release adds a revision).

## Configuration

The plugin reads configuration from the process environment only. It does not look for
`.env` files — core loads its own `.env` into its settings, not into the environment, so a
plugin searching the working directory or `$HOME` would find the wrong file or none.

| Variable | Purpose |
|---|---|
| `RE_DB_URL` | **Required at runtime.** The corpus database. Export it for the process running `research-engine serve` — for MCP clients, in the server's `env` block. `plugin migrate` passes core's configured URL to the migration itself. |
| `ACAD_JOB_LEASE_SECONDS` | How long a job may stay `in_progress` before another worker reclaims it (default 3600). |
| `ACAD_MAX_SNOWBALL_PAPERS` | Budget for papers discovered through citations (default 200). |
| `ACAD_MODULES_DIR` | Optional directory of extra acquisition modules. Every `*.py` file there runs in the server process; there is no default location. |

Provider keys and contact emails are listed under [Providers](#providers). They are never
written to the database: request parameters and error messages are redacted before the API
call log and job errors store them.

## Data and storage

Mutable state lives under the data directory core assigns,
`~/.research-engine/plugin-data/academic-journal/` by default:

```text
plugin-data/academic-journal/
  papers/<paper-id>.pdf     # acquired and manually imported PDFs
```

Nothing is written to the package directory or the working directory. Papers acquired by
0.1.x under `~/.research-engine/papers/` keep their recorded paths and are not moved.

## Database

The plugin owns these tables in the corpus database, outside core's migration chain:

| Table | Holds |
|---|---|
| `acad_papers` | paper metadata, pipeline stage, file path/hash, linked core `document_id`, crawl depth |
| `acad_external_identifiers` | DOI, OpenAlex, Semantic Scholar, arXiv, PMID identifiers |
| `acad_paper_authors` | ordered authors with provider ids |
| `acad_discovery_runs`, `acad_paper_provenance` | which query found which paper, with raw metadata |
| `acad_jobs` | the pipeline job queue |
| `acad_api_calls` | redacted provider call log |
| `acad_pending_citations` | citations waiting for their target to be ingested |
| `acad_schema_migrations` | applied revisions with SHA-256 checksums |

Only `research-engine plugin migrate academic-journal` changes the schema. It holds an
advisory lock, applies each revision in its own transaction, and refuses — without touching
anything — when an applied file's checksum has changed or the database was migrated by a
newer release. There is no downgrade. A database created by 0.1.x migrates in place with
every row kept.

Uninstalling the distribution or running `plugin forget` leaves these tables, the ingested
documents, and their `cites` edges intact.

## Pipeline and workers

```text
discovered → resolved → acquired → ingested → citations_extracted → complete
                ↘ unresolvable
```

Discovery tools insert papers and queue a job; workers do the rest. Workers are asyncio tasks
started by `academic-journal.start_workers` inside the MCP server process:

- **Ownership:** the server process. No subprocess or thread is created, so nothing outlives it.
- **Shutdown:** `academic-journal.stop_workers`, or the server exiting.
- **Concurrency:** one worker per stage per process. Several servers can share a database —
  claims use `FOR UPDATE SKIP LOCKED` and enqueueing is serialised per paper and stage.
- **Restart:** `start_workers` is idempotent and restarts only stopped stages. A job
  interrupted mid-run is reclaimed once its lock is older than `ACAD_JOB_LEASE_SECONDS`.
  A worker that loses the database backs off and keeps polling; `pipeline_status` shows the
  last error.

Failed jobs retry with exponential backoff up to five attempts; `academic-journal.retry_failed`
re-queues them. Citation extraction uses core's extraction service (and therefore core's LLM
configuration) and snowballs through cited DOIs to depth 2 within the snowball budget.

## Manual PDFs and licensing

The plugin only fetches copies that providers report as open access. For anything else,
download the PDF yourself through access you are entitled to and import it:

```text
academic-journal.import_manual_pdf(file_path="/absolute/path/paper.pdf", doi="10.…")
```

The file is copied into the plugin data directory and queued for ingestion. You are
responsible for having the right to store and process it; the plugin does not check licences
or redistribute anything.

## Compatibility

| Plugin | marginalia-ai / marginalia-ai-sdk | Python |
|---|---|---|
| 0.2.x | 0.6.x | ≥ 3.11 |

The public API is the SDK; the plugin imports nothing from `research_engine`.

## Known limitations

- Core 0.6.0 registers post-ingestion hooks but does not call them. The ingestion stage links
  each document to its paper directly, so the pipeline does not depend on the hook.
- DOIs from different providers are matched exactly as stored, so a DOI that differs only in
  letter case can produce two paper rows.

## Privacy and security

Enabled plugins run in the core process with its privileges; enable only distributions you
trust, and review `plugin audit` before approving. Queries, DOIs, and your contact email (when
configured) are sent to the providers above under their terms. Downloaded PDFs and paper
metadata stay in your data directory and database.

## Development

```bash
uv sync --extra dev
uv run ruff check acad tests
uv run pytest tests/unit tests/contract -q
```

Integration tests need the exact core artifact and a disposable PostgreSQL. With Docker
available they start one; otherwise set `ACAD_TEST_DATABASE_URL` to a throwaway database whose
name contains `test`. They never use `RE_DB_URL`, so they cannot reach your research corpus.

```bash
uv sync --extra dev --group integration
uv run pytest tests/integration -q
```

Provider tests that call the real APIs are opt-in: `ACAD_LIVE_TESTS=1 uv run pytest tests/live`.

`scripts/release_smoke.py` exercises a built wheel end to end — enable, migrate, load,
fixture-backed discovery, acquisition, ingestion, and citation edges — against a disposable
database, with the provider APIs and the embedding server stubbed locally:

```bash
RE_DB_URL=postgresql+asyncpg://user:pass@host:5432/acad_smoke_test \
    /path/to/clean-venv/bin/python scripts/release_smoke.py
```

## Support

Report problems at
[issues](https://github.com/John-Cusack/marginalia-plugin-academic-journal/issues). See the
[changelog](CHANGELOG.md) and, for maintainers, [RELEASING.md](RELEASING.md). Licensed under
[Apache-2.0](LICENSE).
