-- Academic literature pipeline tables (plugin-owned, acad_ prefix)
-- Idempotent: all CREATE IF NOT EXISTS

CREATE TABLE IF NOT EXISTS acad_papers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           TEXT NOT NULL,
    abstract        TEXT,
    year            INTEGER,
    venue           TEXT,
    citation_count  INTEGER NOT NULL DEFAULT 0,
    influential_citation_count INTEGER NOT NULL DEFAULT 0,
    open_access_url TEXT,
    file_path       TEXT,
    file_hash       TEXT,
    document_id     UUID,  -- FK to core documents after ingestion
    pipeline_stage  TEXT NOT NULL DEFAULT 'discovered',
    stage_status    TEXT NOT NULL DEFAULT 'pending',
    stage_error     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acad_papers_stage
    ON acad_papers (pipeline_stage, stage_status);
CREATE INDEX IF NOT EXISTS idx_acad_papers_document_id
    ON acad_papers (document_id) WHERE document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS acad_external_identifiers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id    UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_acad_ext_ids_paper
    ON acad_external_identifiers (paper_id);

CREATE TABLE IF NOT EXISTS acad_paper_authors (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id            UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    position            INTEGER NOT NULL,
    openalex_author_id  TEXT,
    s2_author_id        TEXT,
    entity_id           UUID,  -- optional link to core entity
    UNIQUE (paper_id, position)
);

CREATE TABLE IF NOT EXISTS acad_discovery_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_text      TEXT NOT NULL,
    source          TEXT NOT NULL,
    papers_found    INTEGER NOT NULL DEFAULT 0,
    papers_new      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running',
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS acad_paper_provenance (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id         UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    discovery_run_id UUID REFERENCES acad_discovery_runs(id),
    source           TEXT NOT NULL,
    query_text       TEXT,
    rank_in_results  INTEGER,
    raw_metadata     JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acad_provenance_paper
    ON acad_paper_provenance (paper_id);

CREATE TABLE IF NOT EXISTS acad_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    paper_id        UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    last_error      TEXT,
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,
    scheduled_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_acad_jobs_claimable
    ON acad_jobs (stage, priority DESC, created_at ASC)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS acad_api_calls (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    method          TEXT NOT NULL DEFAULT 'GET',
    request_params  JSONB,
    response_status INTEGER,
    response_size   INTEGER,
    error           TEXT,
    duration_ms     INTEGER,
    paper_id        UUID,
    job_id          UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_acad_api_calls_source
    ON acad_api_calls (source, created_at DESC);
