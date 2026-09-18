# Changelog

## 0.2.0 — 2026-09-18

First release as a Python distribution for Research Engine 0.6.

### Packaging

- Named the distribution `marginalia-ai-plugin-academic-journal` and declared the
  `research_engine.plugins` entry point `academic-journal = "acad"`.
- Moved the manifest to `acad/plugin.yaml` (schema v2) and the extraction schema under
  `acad/schemas/`, so both ship in the wheel. Core discovers the plugin without importing it.
- Depends on `marginalia-ai-sdk>=0.6,<0.7` only; every runtime import of `research_engine`
  is gone. Declares `sqlalchemy`, which the filter extension always needed.
- Corrected the licence metadata to Apache-2.0, matching `LICENSE` (0.1.x said MIT).

### Breaking changes

- Tool ids are namespaced by the plugin id, as manifest v2 requires:
  `acad.discover_papers` is now `academic-journal.discover_papers`, and likewise for every tool.
  Source-search ingest actions name the new ids. The source-search provider is still `acad`.
- Tables are created and upgraded only by `research-engine plugin migrate academic-journal`.
  Tools no longer run DDL, and plugin code refuses to run against an unmigrated database.
- `RE_DB_URL` must be in the environment. The `.env` lookup in the working and home
  directories is removed.
- New PDFs go to `PluginContext.data_dir/papers` (default
  `~/.research-engine/plugin-data/academic-journal/papers`). `ACAD_PAPERS_DIR` is removed.
  Existing rows keep their recorded paths.
- User acquisition modules load only from an explicitly set `ACAD_MODULES_DIR`; the implicit
  `~/.research-engine/acad-modules` location is removed.
- `import_manual_pdf` requires an absolute path and copies the file into the data directory.
- `pipeline_status` reports workers per stage with running state and last error.

### Database

- Added the `acad_schema_migrations` ledger with SHA-256 checksums. Upgrades run under an
  advisory lock, one transaction per revision, and refuse checksum drift or a database from a
  newer release. 0.1.x databases migrate in place without data loss.

### Fixes

- Ingestion passed `hint="academic_journal"`, which core reads as an ingestion-module id;
  no such module exists, so every ingest failed with "Unknown module hint". PDFs are now
  dispatched by detection.
- `search_papers` filtered on document type `academic_journal`, which no ingested document
  has, so it returned nothing. It now scopes through the `academic_paper` filter extension.
- Provider API keys and contact emails were stored in `acad_api_calls.request_params` and in
  error text. Both are now redacted before storage and logging.
- A failing provider or unreachable log table made source search raise; it now returns no
  matches and logs why. Matches carry the DOI as a first-class field, so core deduplicates
  them against other providers, and papers already ingested are reported `in_corpus`.
- Concurrent enqueues of the same paper and stage could create duplicate jobs.
- A job held by a process that died stayed `in_progress` forever; it is now reclaimed after
  `ACAD_JOB_LEASE_SECONDS`.
- A database error while claiming jobs ended the worker task silently; workers now back off
  and keep polling.
- `start_workers` restarted nothing while any stage was still alive; it now restarts each
  stopped stage.
- A non-numeric `Retry-After` header crashed the retry loop.
- Acquisition chose the arXiv module for a paper whose only arXiv evidence was its
  open-access URL, then failed with "No arXiv ID found". The identifier is recovered from
  the URL.

### Added

- `academic-journal.stop_workers`.
- Contract, integration (disposable PostgreSQL + core 0.6.0), live-provider, and artifact test
  suites; CI and a Trusted Publishing release workflow.
