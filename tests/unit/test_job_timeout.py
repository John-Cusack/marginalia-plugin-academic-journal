"""A hung job handler must fail the job, never wedge the whole stage."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from acad import config
from acad.infra import job_queue as jq

JobQueue = jq.JobQueue


def _job(job_id: str) -> dict:
    return {"id": job_id, "paper_id": f"paper-{job_id}"}


async def _drive(queue: JobQueue, settle_seconds: float = 0.5) -> None:
    """Run the loop until the stubbed queue is drained, then stop it."""
    task = asyncio.create_task(queue.run())
    await asyncio.sleep(settle_seconds)
    queue.stop()
    await asyncio.wait_for(task, timeout=5)


async def test_hung_handler_fails_its_job_and_stage_moves_on(monkeypatch):
    """Regression: one stuck await used to block the stage task forever."""
    slow, fast = _job("slow"), _job("fast")
    claims = [[slow, fast], []]
    completed: list[str] = []

    async def handler(job: dict) -> None:
        if job["id"] == "slow":
            await asyncio.sleep(30)  # hangs past any sane deadline
        else:
            completed.append(job["id"])

    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "acad.infra.job_queue.db.claim_jobs",
        AsyncMock(side_effect=lambda *a: claims.pop(0) if claims else []),
    )
    monkeypatch.setattr("acad.infra.job_queue.db.complete_job", AsyncMock())

    async def fail_job(job_id: str, error: str) -> None:
        failed.append((job_id, error))

    monkeypatch.setattr("acad.infra.job_queue.db.fail_job", fail_job)

    queue = jq.JobQueue(stage="ingested", handler=handler, poll_interval=0.01, handler_timeout=0.05)
    await _drive(queue)

    assert completed == ["fast"]
    assert [job_id for job_id, _ in failed] == ["slow"]
    assert "timed out after 0.05s" in failed[0][1]
    assert jq.db.complete_job.await_count == 1


async def test_fast_handler_never_hits_the_timeout(monkeypatch):
    monkeypatch.setattr(
        "acad.infra.job_queue.db.claim_jobs",
        AsyncMock(side_effect=[[_job("ok")], []]),
    )
    monkeypatch.setattr("acad.infra.job_queue.db.complete_job", AsyncMock())
    monkeypatch.setattr("acad.infra.job_queue.db.fail_job", AsyncMock())

    queue = jq.JobQueue(
        stage="ingested",
        handler=AsyncMock(),
        poll_interval=0.01,
        handler_timeout=5.0,
    )
    await _drive(queue)

    jq.db.complete_job.assert_awaited_once()
    jq.db.fail_job.assert_not_awaited()


def test_default_timeout_comes_from_config_and_override_wins(monkeypatch):
    monkeypatch.setenv(config.JOB_TIMEOUT_ENV, "60")
    assert JobQueue(stage="ingested", handler=AsyncMock()).handler_timeout == 60.0
    assert (
        JobQueue(stage="ingested", handler=AsyncMock(), handler_timeout=7.5).handler_timeout == 7.5
    )


async def test_timeout_raised_by_the_handler_is_not_the_job_deadline(monkeypatch):
    """A timeout from inside the handler must not be reported as a hung handler."""
    failed: list[tuple[str, str]] = []

    async def handler(job: dict) -> None:
        raise TimeoutError("read timeout from publisher")

    async def fail_job(job_id: str, error: str) -> None:
        failed.append((job_id, error))

    monkeypatch.setattr(
        "acad.infra.job_queue.db.claim_jobs",
        AsyncMock(side_effect=[[_job("inner")], []]),
    )
    monkeypatch.setattr("acad.infra.job_queue.db.complete_job", AsyncMock())
    monkeypatch.setattr("acad.infra.job_queue.db.fail_job", fail_job)

    queue = jq.JobQueue(
        stage="ingested",
        handler=handler,
        poll_interval=0.01,
        handler_timeout=5.0,
    )
    await _drive(queue)

    assert [job_id for job_id, _ in failed] == ["inner"]
    message = failed[0][1]
    assert "read timeout from publisher" in message
    assert "timed out after" not in message
    assert config.JOB_TIMEOUT_ENV not in message


async def test_timeout_while_recording_the_outcome_is_not_a_hung_handler(monkeypatch):
    """A statement timeout while completing a job is not a hung handler."""
    failed: list[tuple[str, str]] = []

    async def complete_job(job_id: str) -> None:
        raise TimeoutError("statement timed out")

    async def fail_job(job_id: str, error: str) -> None:
        failed.append((job_id, error))

    monkeypatch.setattr(
        "acad.infra.job_queue.db.claim_jobs",
        AsyncMock(side_effect=[[_job("db")], []]),
    )
    monkeypatch.setattr("acad.infra.job_queue.db.complete_job", complete_job)
    monkeypatch.setattr("acad.infra.job_queue.db.fail_job", fail_job)

    queue = jq.JobQueue(
        stage="ingested",
        handler=AsyncMock(),
        poll_interval=0.01,
        handler_timeout=5.0,
    )
    await _drive(queue)

    assert [job_id for job_id, _ in failed] == ["db"]
    assert "timed out after" not in failed[0][1]


async def test_stage_survives_a_failure_to_record_the_outcome(monkeypatch):
    """A database outage while failing a job must not end the worker task."""
    claims = [[_job("broken"), _job("healthy")], []]

    async def handler(job: dict) -> None:
        if job["id"] == "broken":
            raise ValueError("handler blew up")

    async def fail_job(job_id: str, error: str) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(
        "acad.infra.job_queue.db.claim_jobs",
        AsyncMock(side_effect=lambda *a: claims.pop(0) if claims else []),
    )
    monkeypatch.setattr("acad.infra.job_queue.db.complete_job", AsyncMock())
    monkeypatch.setattr("acad.infra.job_queue.db.fail_job", fail_job)

    queue = jq.JobQueue(
        stage="ingested",
        handler=handler,
        poll_interval=0.01,
        handler_timeout=5.0,
    )
    await _drive(queue)

    jq.db.complete_job.assert_awaited_once_with("healthy")
