"""In-process worker ownership, shutdown, and restart semantics."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from acad.infra import job_queue, worker


@pytest.fixture
async def no_jobs(monkeypatch):
    """Workers that poll an always-empty queue, and a clean registry after each test."""
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(job_queue.db, "claim_jobs", claim)
    yield claim
    await worker.stop_workers()


async def test_start_is_idempotent(no_jobs):
    assert await worker.start_workers() == 4
    tasks = dict(worker._tasks)

    assert await worker.start_workers() == 0
    assert worker._tasks == tasks
    status = worker.worker_status()
    assert status["active_workers"] == 4
    assert {s["stage"] for s in status["stages"]} == {
        "resolved", "acquired", "ingested", "citations_extracted",
    }


async def test_start_restarts_only_stopped_stage(no_jobs):
    await worker.start_workers()
    survivors = {stage: task for stage, task in worker._tasks.items() if stage != "ingested"}
    worker._tasks["ingested"].cancel()
    await asyncio.sleep(0)

    assert worker.worker_status()["active_workers"] == 3
    assert await worker.start_workers() == 1
    assert all(worker._tasks[stage] is task for stage, task in survivors.items())
    assert worker.worker_status()["active_workers"] == 4


async def test_stop_cancels_every_task_and_waits(no_jobs):
    await worker.start_workers()
    tasks = list(worker._tasks.values())

    assert await worker.stop_workers() == 4
    assert all(task.done() for task in tasks)
    assert worker.worker_status() == {"total_workers": 0, "active_workers": 0, "stages": []}


async def test_worker_survives_claim_errors_and_reports_them(monkeypatch):
    calls = 0
    recovered = asyncio.Event()

    async def flaky_claim(stage, limit, worker_id):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("connection to postgresql://u:hunter2@db/x refused")
        recovered.set()
        return []

    monkeypatch.setattr(job_queue.db, "claim_jobs", flaky_claim)
    queue = job_queue.JobQueue("resolved", AsyncMock(), poll_interval=0.001)
    task = asyncio.create_task(queue.run())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=2)
        assert not task.done()
    finally:
        queue.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert calls >= 3


async def test_claim_error_is_redacted_in_status(monkeypatch):
    blocked = asyncio.Event()

    async def failing_claim(stage, limit, worker_id):
        blocked.set()
        raise ConnectionError("postgresql://u:hunter2@db/x refused")

    monkeypatch.setattr(job_queue.db, "claim_jobs", failing_claim)
    monkeypatch.setattr(job_queue, "_MAX_CLAIM_BACKOFF", 10.0)
    try:
        await worker.start_workers()
        await asyncio.wait_for(blocked.wait(), timeout=2)
        await asyncio.sleep(0)
        errors = [s.get("last_claim_error", "") for s in worker.worker_status()["stages"]]
        assert any("refused" in e for e in errors)
        assert not any("hunter2" in e for e in errors)
    finally:
        await worker.stop_workers()


async def test_failed_job_error_is_redacted(monkeypatch):
    fail = AsyncMock()
    monkeypatch.setattr(job_queue.db, "fail_job", fail)
    job = {"id": "j1", "paper_id": "p1"}
    monkeypatch.setattr(
        job_queue.db, "claim_jobs", AsyncMock(side_effect=[[job], asyncio.CancelledError()])
    )

    async def handler(_job):
        raise RuntimeError("403 for https://api.core.ac.uk/v3?api_key=TOPSECRET")

    queue = job_queue.JobQueue("resolved", handler, poll_interval=0.001)
    with pytest.raises(asyncio.CancelledError):
        await queue.run()

    error = fail.await_args.args[1]
    assert "TOPSECRET" not in error
    assert "403" in error
