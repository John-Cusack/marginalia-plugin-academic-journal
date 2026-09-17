"""All SQL query functions for the academic literature pipeline."""

from __future__ import annotations

import json
import random
from typing import TYPE_CHECKING, Any

from acad import config
from acad.db.pool import get_pool

if TYPE_CHECKING:
    from uuid import UUID

# ---------- Papers ----------


async def insert_paper(data: dict[str, Any]) -> UUID:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO acad_papers
                (title, abstract, year, venue, citation_count,
                 influential_citation_count, open_access_url, pipeline_stage, stage_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id""",
            data["title"],
            data.get("abstract"),
            data.get("year"),
            data.get("venue"),
            data.get("citation_count", 0),
            data.get("influential_citation_count", 0),
            data.get("open_access_url"),
            data.get("pipeline_stage", "discovered"),
            data.get("stage_status", "pending"),
        )
        return row["id"]


async def get_paper(paper_id: UUID) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM acad_papers WHERE id = $1", paper_id)
        return dict(row) if row else None


async def find_paper_by_external_id(source: str, external_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.* FROM acad_papers p
            JOIN acad_external_identifiers ei ON ei.paper_id = p.id
            WHERE ei.source = $1 AND ei.external_id = $2""",
            source, external_id,
        )
        return dict(row) if row else None


async def update_paper_stage(
    paper_id: UUID, stage: str, status: str, error: str | None = None
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE acad_papers
            SET pipeline_stage = $1, stage_status = $2,
                stage_error = $3, updated_at = NOW()
            WHERE id = $4""",
            stage, status, error, paper_id,
        )


async def update_paper_file(paper_id: UUID, file_path: str, file_hash: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acad_papers SET file_path = $1, file_hash = $2, updated_at = NOW() WHERE id = $3",
            file_path, file_hash, paper_id,
        )


async def update_paper_document_id(paper_id: UUID, document_id: UUID) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acad_papers SET document_id = $1, updated_at = NOW() WHERE id = $2",
            document_id, paper_id,
        )


async def update_paper_oa_url(paper_id: UUID, url: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acad_papers SET open_access_url = $1, updated_at = NOW() WHERE id = $2",
            url, paper_id,
        )


# ---------- Citation graph / snowballing ----------


async def set_crawl_depth(paper_id: UUID, depth: int) -> None:
    """Record a snowballed paper's depth (only set for non-seed papers)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acad_papers SET crawl_depth = $1, updated_at = NOW() WHERE id = $2",
            depth, paper_id,
        )


async def count_snowballed_papers() -> int:
    """Count papers created by snowballing (crawl_depth IS NOT NULL)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM acad_papers WHERE crawl_depth IS NOT NULL"
        ) or 0


async def insert_pending_citation(
    citing_paper_id: UUID,
    cited_paper_id: UUID,
    attributes: dict | None = None,
    confidence: float = 1.0,
) -> None:
    """Record a citation whose target isn't ingested yet (no document_id)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO acad_pending_citations
                (citing_paper_id, cited_paper_id, attributes, confidence)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (citing_paper_id, cited_paper_id) DO NOTHING""",
            citing_paper_id, cited_paper_id,
            json.dumps(attributes or {}), confidence,
        )


async def get_pending_citations_for_cited(cited_paper_id: UUID) -> list[dict]:
    """Pending citations that target the given (now-ingested) paper."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT pc.citing_paper_id, pc.cited_paper_id,
                      pc.attributes, pc.confidence, p.document_id AS citing_document_id
            FROM acad_pending_citations pc
            JOIN acad_papers p ON p.id = pc.citing_paper_id
            WHERE pc.cited_paper_id = $1""",
            cited_paper_id,
        )
        result = []
        for r in rows:
            d = dict(r)
            # asyncpg returns JSONB as a string; decode for downstream consumers.
            if isinstance(d.get("attributes"), str):
                d["attributes"] = json.loads(d["attributes"])
            result.append(d)
        return result


async def delete_pending_citation(citing_paper_id: UUID, cited_paper_id: UUID) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM acad_pending_citations WHERE citing_paper_id = $1 AND cited_paper_id = $2",
            citing_paper_id, cited_paper_id,
        )


# ---------- External Identifiers ----------


async def insert_external_id(paper_id: UUID, source: str, external_id: str) -> UUID | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO acad_external_identifiers (paper_id, source, external_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (source, external_id) DO NOTHING
            RETURNING id""",
            paper_id, source, external_id,
        )
        return row["id"] if row else None


async def get_external_ids(paper_id: UUID) -> dict[str, str]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source, external_id FROM acad_external_identifiers WHERE paper_id = $1",
            paper_id,
        )
        return {r["source"]: r["external_id"] for r in rows}


# ---------- Authors ----------


async def insert_paper_authors(paper_id: UUID, authors: list[dict]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        for author in authors:
            await conn.execute(
                """INSERT INTO acad_paper_authors (paper_id, name, position, openalex_author_id, s2_author_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (paper_id, position) DO NOTHING""",
                paper_id,
                author["name"],
                author["position"],
                author.get("openalex_author_id"),
                author.get("s2_author_id"),
            )


# ---------- Discovery Runs ----------


async def insert_discovery_run(query_text: str, source: str = "openalex") -> UUID:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO acad_discovery_runs (query_text, source)
            VALUES ($1, $2)
            RETURNING id""",
            query_text, source,
        )
        return row["id"]


async def complete_discovery_run(
    run_id: UUID, found: int, new: int, error: str | None = None
) -> None:
    status = "failed" if error else "completed"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE acad_discovery_runs
            SET papers_found = $1, papers_new = $2, status = $3, error = $4, completed_at = NOW()
            WHERE id = $5""",
            found, new, status, error, run_id,
        )


# ---------- Provenance ----------


async def insert_provenance(data: dict[str, Any]) -> UUID:
    pool = await get_pool()
    async with pool.acquire() as conn:
        raw_metadata = data.get("raw_metadata")
        row = await conn.fetchrow(
            """INSERT INTO acad_paper_provenance
                (paper_id, discovery_run_id, source, query_text, rank_in_results, raw_metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id""",
            data["paper_id"],
            data.get("discovery_run_id"),
            data["source"],
            data.get("query_text"),
            data.get("rank_in_results"),
            json.dumps(raw_metadata) if raw_metadata else None,
        )
        return row["id"]


# ---------- API Call Log ----------


async def log_api_call(data: dict[str, Any]) -> UUID:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO acad_api_calls
                (source, endpoint, method, request_params, response_status,
                 response_size, error, duration_ms, paper_id, job_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id""",
            data["source"],
            data["endpoint"],
            data.get("method", "GET"),
            json.dumps(config.redact_params(data["request_params"]))
            if data.get("request_params")
            else None,
            data.get("response_status"),
            data.get("response_size"),
            config.redact_text(data["error"]) if data.get("error") else None,
            data.get("duration_ms"),
            data.get("paper_id"),
            data.get("job_id"),
        )
        return row["id"]


# ---------- Job Queue ----------


async def create_job(paper_id: UUID, stage: str, priority: int = 0) -> UUID:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        # Dedup: return the existing pending job. The advisory lock serialises
        # concurrent enqueues of one (paper, stage); without it two callers both
        # miss the SELECT and both INSERT.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"acad_jobs:{paper_id}:{stage}",
        )
        existing = await conn.fetchrow(
            "SELECT id FROM acad_jobs WHERE paper_id = $1 AND stage = $2 AND status = 'pending'",
            paper_id, stage,
        )
        if existing:
            return existing["id"]

        row = await conn.fetchrow(
            """INSERT INTO acad_jobs (paper_id, stage, priority)
            VALUES ($1, $2, $3)
            RETURNING id""",
            paper_id, stage, priority,
        )
        return row["id"]


async def claim_jobs(
    stage: str, limit: int, worker_id: str, lease_seconds: float | None = None
) -> list[dict]:
    """Claim pending jobs using SELECT FOR UPDATE SKIP LOCKED.

    A job whose lock is older than the lease is claimable again: the worker that
    held it belonged to a process that stopped (server restart, crash) without
    completing or failing it, and nothing else would ever release it.
    """
    lease = config.job_lease_seconds() if lease_seconds is None else lease_seconds
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """UPDATE acad_jobs
            SET status = 'in_progress',
                locked_at = NOW(),
                locked_by = $1
            WHERE id IN (
                SELECT id FROM acad_jobs
                WHERE stage = $2
                  AND (
                    (status = 'pending' AND scheduled_after <= NOW())
                    OR (status = 'in_progress'
                        AND locked_at < NOW() - make_interval(secs => $4))
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT $3
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *""",
            worker_id, stage, limit, float(lease),
        )
        return [dict(r) for r in rows]


async def complete_job(job_id: UUID) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE acad_jobs
            SET status = 'succeeded', completed_at = NOW()
            WHERE id = $1""",
            job_id,
        )


async def fail_job(job_id: UUID, error: str) -> None:
    """Increment attempts and apply exponential backoff with jitter."""
    error = config.redact_text(error)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempts, max_attempts FROM acad_jobs WHERE id = $1",
            job_id,
        )
        if not row:
            return

        new_attempts = row["attempts"] + 1
        if new_attempts >= row["max_attempts"]:
            await conn.execute(
                """UPDATE acad_jobs
                SET status = 'failed', attempts = $1,
                    last_error = $2, completed_at = NOW()
                WHERE id = $3""",
                new_attempts, error, job_id,
            )
        else:
            backoff = (2 ** new_attempts) * (0.5 + random.random())
            await conn.execute(
                """UPDATE acad_jobs
                SET status = 'pending', attempts = $1,
                    last_error = $2, locked_at = NULL, locked_by = NULL,
                    scheduled_after = NOW() + make_interval(secs => $3)
                WHERE id = $4""",
                new_attempts, error, backoff, job_id,
            )


async def defer_job(job_id: UUID, delay_seconds: float) -> None:
    """Reschedule a job without incrementing attempts (e.g., circuit breaker open)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE acad_jobs
            SET status = 'pending',
                locked_at = NULL, locked_by = NULL,
                scheduled_after = NOW() + make_interval(secs => $1)
            WHERE id = $2""",
            delay_seconds, job_id,
        )


async def retry_failed_jobs(stage: str | None = None, error_pattern: str | None = None) -> int:
    """Re-enqueue failed jobs. Returns count of jobs re-enqueued."""
    pool = await get_pool()
    conditions = ["status = 'failed'"]
    params: list[Any] = []
    idx = 1

    if stage:
        conditions.append(f"stage = ${idx}")
        params.append(stage)
        idx += 1
    if error_pattern:
        conditions.append(f"last_error LIKE ${idx}")
        params.append(f"%{error_pattern}%")
        idx += 1

    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""UPDATE acad_jobs
            SET status = 'pending', attempts = 0,
                last_error = NULL, locked_at = NULL, locked_by = NULL,
                scheduled_after = NOW(), completed_at = NULL
            WHERE {where}""",
            *params,
        )
        # asyncpg returns "UPDATE N"
        return int(result.split()[-1])


# ---------- Pipeline Status ----------


async def pipeline_status() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        stage_counts = await conn.fetch(
            """SELECT pipeline_stage, stage_status, COUNT(*) as cnt
            FROM acad_papers GROUP BY pipeline_stage, stage_status
            ORDER BY pipeline_stage, stage_status"""
        )

        queue_depth = await conn.fetch(
            """SELECT stage, status, COUNT(*) as cnt
            FROM acad_jobs
            WHERE status IN ('pending', 'in_progress')
            GROUP BY stage, status"""
        )

        recent_errors = await conn.fetch(
            """SELECT stage, last_error, COUNT(*) as cnt
            FROM acad_jobs
            WHERE status = 'failed'
              AND completed_at >= NOW() - INTERVAL '24 hours'
            GROUP BY stage, last_error
            ORDER BY cnt DESC
            LIMIT 5"""
        )

        source_health = await conn.fetch(
            """SELECT source,
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE error IS NOT NULL) as error_count,
                ROUND(AVG(duration_ms)::numeric, 0) as avg_duration_ms
            FROM acad_api_calls
            WHERE created_at >= NOW() - INTERVAL '1 hour'
            GROUP BY source"""
        )

        total_papers = await conn.fetchval("SELECT COUNT(*) FROM acad_papers")

    return {
        "total_papers": total_papers or 0,
        "papers_by_stage": [dict(r) for r in stage_counts],
        "queue_depth": [dict(r) for r in queue_depth],
        "recent_errors": [dict(r) for r in recent_errors],
        "source_health": [dict(r) for r in source_health],
    }
