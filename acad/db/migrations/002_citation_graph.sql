-- Citation graph / snowballing support.
-- crawl_depth: NULL = a seed paper (user-discovered, depth 0). Snowballed
-- papers get an explicit depth = parent.depth + 1, used to cap recursion.
ALTER TABLE acad_papers ADD COLUMN IF NOT EXISTS crawl_depth INT;

CREATE INDEX IF NOT EXISTS idx_acad_papers_crawl_depth
    ON acad_papers (crawl_depth) WHERE crawl_depth IS NOT NULL;

-- Citations whose target paper is known but not yet ingested (no document_id).
-- Flushed into core `cites` edges when the cited paper reaches `complete`.
CREATE TABLE IF NOT EXISTS acad_pending_citations (
    citing_paper_id UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    cited_paper_id  UUID NOT NULL REFERENCES acad_papers(id) ON DELETE CASCADE,
    attributes      JSONB DEFAULT '{}',
    confidence      REAL DEFAULT 1.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (citing_paper_id, cited_paper_id)
);

CREATE INDEX IF NOT EXISTS idx_acad_pending_citations_cited
    ON acad_pending_citations (cited_paper_id);
