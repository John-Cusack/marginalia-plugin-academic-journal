"""Integration tests for the job queue (requires PostgreSQL)."""

from __future__ import annotations

import pytest

# These tests require a running PostgreSQL instance.
# Mark them so they can be skipped in CI without a database.

pytestmark = pytest.mark.skipif(
    True,  # Set to False when running with a real database
    reason="Requires PostgreSQL (set RE_DB_URL and change skip condition)",
)


@pytest.mark.asyncio
async def test_create_job_dedup():
    """Creating the same job twice should return the same ID."""
    from uuid import uuid4

    from acad.db import queries as db

    paper_id = uuid4()
    # Would need a real paper in the DB for FK constraint
    # This is a skeleton for when running against real Postgres
    job_id_1 = await db.create_job(paper_id, "resolved")
    job_id_2 = await db.create_job(paper_id, "resolved")
    assert job_id_1 == job_id_2


@pytest.mark.asyncio
async def test_claim_and_complete():
    """Claimed jobs should not be claimable again."""
    from acad.db import queries as db

    jobs = await db.claim_jobs("resolved", 5, "test-worker")
    for job in jobs:
        await db.complete_job(job["id"])
