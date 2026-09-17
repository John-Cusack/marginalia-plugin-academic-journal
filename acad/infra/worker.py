"""Asyncio worker orchestration for pipeline stages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)

# Global registry of running worker tasks
_worker_tasks: list[asyncio.Task] = []
_workers: list[JobQueue] = []


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
    """Launch background pipeline workers. Returns number of workers started."""
    global _worker_tasks, _workers

    if _worker_tasks:
        # Already running
        active = sum(1 for t in _worker_tasks if not t.done())
        if active > 0:
            logger.info("Workers already running (%d active)", active)
            return active

    _worker_tasks.clear()
    _workers.clear()

    handlers = _get_handlers()
    for stage, config in handlers.items():
        worker = JobQueue(
            stage=stage,
            handler=config["handler"],
            batch_size=config["batch_size"],
            poll_interval=config["poll_interval"],
        )
        _workers.append(worker)
        task = asyncio.create_task(worker.run(), name=f"acad-worker-{stage}")
        _worker_tasks.append(task)
        logger.info("Started worker for stage=%s", stage)

    return len(_worker_tasks)


async def stop_workers() -> int:
    """Gracefully stop all workers. Returns number stopped."""
    global _worker_tasks, _workers
    count = 0

    for worker in _workers:
        worker.stop()

    for task in _worker_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            count += 1

    _worker_tasks.clear()
    _workers.clear()
    logger.info("Stopped %d workers", count)
    return count


def worker_status() -> dict:
    """Return current worker status."""
    return {
        "total_workers": len(_worker_tasks),
        "active_workers": sum(1 for t in _worker_tasks if not t.done()),
        "stages": [w.stage for w in _workers],
    }
