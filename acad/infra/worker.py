"""Asyncio worker orchestration for pipeline stages.

Lifecycle, since core 0.6 has no plugin start/stop hooks:

- **Ownership.** Workers are asyncio tasks inside the process whose event loop ran
  ``academic-journal.start_workers`` — the MCP server. No subprocess or thread is
  created, so nothing can outlive that process.
- **Shutdown.** ``academic-journal.stop_workers`` cancels them; otherwise they end with
  the server's event loop.
- **Concurrency.** One task per stage per process. Several processes may run workers
  against one database: claims use ``FOR UPDATE SKIP LOCKED``, and enqueueing takes a
  per-(paper, stage) advisory lock, so no job is claimed or queued twice.
- **Restart.** ``start_workers`` is idempotent and restarts only stages whose task has
  stopped. A job left ``in_progress`` by a process that died is reclaimed once its lock
  is older than ``ACAD_JOB_LEASE_SECONDS`` (default one hour).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from acad import config
from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)

# Running worker tasks, keyed by stage.
_tasks: dict[str, asyncio.Task] = {}
_workers: dict[str, JobQueue] = {}


def _get_handlers() -> dict[str, dict[str, Any]]:
    """Return worker configurations for each pipeline stage."""
    from acad.pipeline.acquisition import acquire_handler
    from acad.pipeline.citation_extraction import citation_extraction_handler
    from acad.pipeline.ingestion import ingestion_handler
    from acad.pipeline.resolution import resolve_handler

    return {
        "resolved": {
            "handler": resolve_handler,
            "batch_size": 10,
            "poll_interval": 2.0,
        },
        "acquired": {
            "handler": acquire_handler,
            "batch_size": 5,
            "poll_interval": 3.0,
        },
        "ingested": {
            "handler": ingestion_handler,
            "batch_size": 3,
            "poll_interval": 5.0,
        },
        "citations_extracted": {
            "handler": citation_extraction_handler,
            "batch_size": 2,
            "poll_interval": 10.0,
        },
    }


async def start_workers() -> int:
    """Start a worker for every stage not already running. Returns how many started."""
    started = 0
    for stage, cfg in _get_handlers().items():
        task = _tasks.get(stage)
        if task is not None and not task.done():
            continue
        worker = JobQueue(
            stage=stage,
            handler=cfg["handler"],
            batch_size=cfg["batch_size"],
            poll_interval=cfg["poll_interval"],
        )
        _workers[stage] = worker
        _tasks[stage] = asyncio.create_task(
            worker.run(), name=f"academic-journal-worker-{stage}"
        )
        started += 1
        logger.info("Started worker for stage=%s", stage)
    return started


async def stop_workers() -> int:
    """Stop all workers and wait for them to finish. Returns how many were running."""
    count = 0
    for worker in _workers.values():
        worker.stop()
    for task in _tasks.values():
        if not task.done():
            task.cancel()
            count += 1
    if _tasks:
        await asyncio.gather(*_tasks.values(), return_exceptions=True)
    _tasks.clear()
    _workers.clear()
    logger.info("Stopped %d workers", count)
    return count


def worker_status() -> dict:
    """Return current worker status, including why a stopped worker stopped."""
    stages = []
    for stage, task in _tasks.items():
        entry: dict[str, Any] = {"stage": stage, "running": not task.done()}
        worker = _workers.get(stage)
        if worker is not None and worker.last_claim_error:
            entry["last_claim_error"] = worker.last_claim_error
        if task.done() and not task.cancelled() and task.exception() is not None:
            entry["error"] = config.redact_text(repr(task.exception()))
        stages.append(entry)
    return {
        "total_workers": len(_tasks),
        "active_workers": sum(1 for task in _tasks.values() if not task.done()),
        "stages": stages,
    }
