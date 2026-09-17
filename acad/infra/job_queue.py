"""Job queue over PostgreSQL SELECT FOR UPDATE SKIP LOCKED."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from acad import config
from acad.db import queries as db
from acad.infra.circuit_breaker import CircuitOpenError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_MAX_CLAIM_BACKOFF = 60.0


class JobQueue:
    def __init__(
        self,
        stage: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        batch_size: int = 5,
        poll_interval: float = 2.0,
    ) -> None:
        self.stage = stage
        self.handler = handler
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.worker_id = f"{stage}-{uuid.uuid4().hex[:8]}"
        self.last_claim_error: str | None = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        processed = 0
        claim_failures = 0
        logger.info("Worker %s started for stage=%s", self.worker_id, self.stage)

        while self._running:
            try:
                jobs = await db.claim_jobs(self.stage, self.batch_size, self.worker_id)
            except Exception as exc:
                # A database outage must not end the task: nothing would restart it
                # until someone called start_workers again.
                claim_failures += 1
                self.last_claim_error = config.redact_text(str(exc)) or type(exc).__name__
                delay = min(self.poll_interval * 2**claim_failures, _MAX_CLAIM_BACKOFF)
                logger.warning(
                    "Worker %s could not claim jobs (retry in %.0fs): %s",
                    self.worker_id, delay, self.last_claim_error,
                )
                await asyncio.sleep(delay)
                continue
            claim_failures = 0
            self.last_claim_error = None

            if not jobs:
                if processed > 0:
                    logger.info(
                        "Worker %s: no more jobs (processed %d total)",
                        self.stage, processed,
                    )
                    processed = 0
                await asyncio.sleep(self.poll_interval)
                continue

            for job in jobs:
                try:
                    await self.handler(job)
                    await db.complete_job(job["id"])
                    processed += 1
                    if processed % 10 == 0:
                        logger.info("Worker %s: processed %d jobs", self.stage, processed)
                except CircuitOpenError as exc:
                    logger.info(
                        "Job %s deferred (circuit open for %s)", job["id"], exc.source
                    )
                    await db.defer_job(job["id"], delay_seconds=exc.retry_after + 5)
                except Exception as exc:
                    error = config.redact_text(str(exc)) or type(exc).__name__
                    logger.error(
                        "Job %s (paper %s) failed: %s",
                        job["id"], job["paper_id"], error,
                    )
                    await db.fail_job(job["id"], error)

    def stop(self) -> None:
        self._running = False
        logger.info("Worker %s stopping", self.worker_id)

    @staticmethod
    async def enqueue(paper_id: Any, stage: str, priority: int = 0) -> Any:
        return await db.create_job(paper_id, stage, priority)
