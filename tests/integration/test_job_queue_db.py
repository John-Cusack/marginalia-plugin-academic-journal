"""The job queue against PostgreSQL: dedup, claiming, leases, retries."""

from __future__ import annotations

import asyncio

import pytest

from acad.db import queries as db

pytestmark = pytest.mark.integration


async def _paper(title: str = "Paper") -> str:
    return await db.insert_paper({"title": title})


async def test_create_job_dedups_pending_work(migrated):
    paper_id = await _paper()
    first = await db.create_job(paper_id, "resolved")
    second = await db.create_job(paper_id, "resolved")
    assert first == second


async def test_concurrent_create_job_makes_one_job(migrated):
    paper_id = await _paper()

    ids = await asyncio.gather(*(db.create_job(paper_id, "resolved") for _ in range(8)))

    assert len(set(ids)) == 1
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM acad_jobs") == 1


async def test_claims_are_disjoint_across_workers(migrated):
    for index in range(6):
        await db.create_job(await _paper(f"P{index}"), "acquired")

    first, second = await asyncio.gather(
        db.claim_jobs("acquired", 4, "worker-a"),
        db.claim_jobs("acquired", 4, "worker-b"),
    )

    ids_a = {row["id"] for row in first}
    ids_b = {row["id"] for row in second}
    assert not ids_a & ids_b
    assert len(ids_a) + len(ids_b) == 6


async def test_claimed_job_is_not_reclaimed_within_its_lease(migrated):
    await db.create_job(await _paper(), "acquired")
    claimed = await db.claim_jobs("acquired", 5, "worker-a")
    assert len(claimed) == 1

    assert await db.claim_jobs("acquired", 5, "worker-b") == []


async def test_job_orphaned_by_a_dead_worker_is_reclaimed_after_the_lease(migrated):
    """The worker that held this job belonged to a process that stopped."""
    await db.create_job(await _paper(), "acquired")
    [claimed] = await db.claim_jobs("acquired", 5, "worker-gone")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE acad_jobs SET locked_at = NOW() - INTERVAL '2 hours' WHERE id = $1",
            claimed["id"],
        )

    reclaimed = await db.claim_jobs("acquired", 5, "worker-new")

    assert [row["id"] for row in reclaimed] == [claimed["id"]]
    assert reclaimed[0]["locked_by"] == "worker-new"


async def test_failure_backs_off_then_exhausts_attempts(migrated):
    job_id = await db.create_job(await _paper(), "resolved")
    pool = await db.get_pool()

    await db.fail_job(job_id, "provider down")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, attempts, last_error, scheduled_after > NOW() AS deferred "
            "FROM acad_jobs WHERE id = $1",
            job_id,
        )
    assert (row["status"], row["attempts"], row["deferred"]) == ("pending", 1, True)
    assert row["last_error"] == "provider down"

    for _ in range(4):
        await db.fail_job(job_id, "provider down")
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM acad_jobs WHERE id = $1", job_id) == "failed"

    assert await db.retry_failed_jobs(stage="resolved") == 1
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, attempts FROM acad_jobs WHERE id = $1", job_id)
    assert (row["status"], row["attempts"]) == ("pending", 0)


async def test_failed_job_error_is_stored_redacted(migrated):
    job_id = await db.create_job(await _paper(), "resolved")

    await db.fail_job(job_id, "403 for https://api.core.ac.uk/v3/search?api_key=SECRETVALUE")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        stored = await conn.fetchval("SELECT last_error FROM acad_jobs WHERE id = $1", job_id)
    assert "SECRETVALUE" not in stored
    assert "403" in stored


async def test_api_call_log_never_stores_credentials(migrated):
    await db.log_api_call({
        "source": "ncbi",
        "endpoint": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
        "request_params": {"id": "12345", "api_key": "NCBI-SECRET"},
        "response_status": 200,
        "error": "failed for url '...?api_key=NCBI-SECRET'",
    })

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT request_params::text AS params, error FROM acad_api_calls"
        )
    assert "NCBI-SECRET" not in row["params"]
    assert "NCBI-SECRET" not in row["error"]
    assert '"id": "12345"' in row["params"]
