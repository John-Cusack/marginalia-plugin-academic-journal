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


class HandlerDeadlineExceeded(Exception):
    """The job handler ran past the worker's deadline.

    Distinct from a ``TimeoutError`` the handler raised itself - an HTTP
    client read timeout, a database statement. Only this one means the
    handler hung.
    """

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        super().__init__(
            f"handler timed out after {timeout:g}s (see {config.JOB_TIMEOUT_ENV})"
        )


class JobQueue:
    def __init__(
        self,
        stage: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        batch_size: int = 5,
        poll_interval: float = 2.0,
        handler_timeout: float | None = None,
    ) -> None:
        self.stage = stage
        self.handler = handler
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        # A hung handler must never wedge the stage: without a deadline one
        # stuck await blocks this whole task, and the claimed batch behind it.
        self.handler_timeout = (
            config.job_timeout_seconds() if handler_timeout is None else handler_timeout
        )
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
                    completed = await self._process_job(job)
                except Exception as exc:
                    # The outcome could not be recorded - a database outage
                    # while completing, failing or deferring the job. The job
                    # stays in_progress and lease reclaim returns it to the
                    # pool; the stage keeps moving.
                    logger.error(
                        "Job %s (paper %s) outcome not recorded: %s",
                        job["id"], job["paper_id"],
                        config.redact_text(str(exc)) or type(exc).__name__,
                    )
                    continue
                if completed:
                    processed += 1
                    if processed % 10 == 0:
                        logger.info("Worker %s: processed %d jobs", self.stage, processed)

    async def _run_handler(self, job: dict[str, Any]) -> None:
        """Run one job handler under the worker's deadline.

        Raises :class:`HandlerDeadlineExceeded` when the deadline expired. A
        timeout the handler raised itself - an HTTP client read timeout, a
        database statement - is left as it is: it is a different failure and
        must not be reported as a hung handler.
        """
        scope = asyncio.timeout(self.handler_timeout)
        try:
            async with scope:
                await self.handler(job)
        except TimeoutError as exc:
            # asyncio.timeout turns the cancellation it caused into a
            # TimeoutError; a timeout raised by the handler arrives here
            # unchanged. scope.expired() tells the two apart.
            if scope.expired():
                raise HandlerDeadlineExceeded(self.handler_timeout) from exc
            raise

    async def _process_job(self, job: dict[str, Any]) -> bool:
        """Run one claimed job and record its outcome. True when it completed."""
        try:
            await self._run_handler(job)
        except CircuitOpenError as exc:
            logger.info(
                "Job %s deferred (circuit open for %s)", job["id"], exc.source
            )
            await db.defer_job(job["id"], delay_seconds=exc.retry_after + 5)
            return False
        except HandlerDeadlineExceeded as exc:
            # The handler hung: fail it (backoff, then failed after
            # max_attempts) so the stage keeps moving. Lease reclaim is
            # the backstop for jobs orphaned by a killed process, not
            # for a live-but-stuck handler.
            logger.error("Job %s (paper %s) %s", job["id"], job["paper_id"], exc)
            await db.fail_job(job["id"], str(exc))
            return False
        except TimeoutError as exc:
            # The handler timed out on its own - an HTTP client read timeout,
            # a database statement. This is not the job deadline: report the
            # real cause, or the deadline message hides the actual fault.
            detail = config.redact_text(str(exc))
            error = (
                f"handler raised TimeoutError: {detail}"
                if detail
                else "handler raised TimeoutError"
            )
            logger.error("Job %s (paper %s) failed: %s", job["id"], job["paper_id"], error)
            await db.fail_job(job["id"], error)
            return False
        except Exception as exc:
            error = config.redact_text(str(exc)) or type(exc).__name__
            logger.error(
                "Job %s (paper %s) failed: %s",
                job["id"], job["paper_id"], error,
            )
            await db.fail_job(job["id"], error)
            return False

        try:
            await db.complete_job(job["id"])
        except Exception as exc:
            # The handler ran, but the outcome could not be stored: retry the
            # job through the failure path.
            error = config.redact_text(str(exc)) or type(exc).__name__
            logger.error(
                "Job %s (paper %s) could not be completed: %s",
                job["id"], job["paper_id"], error,
            )
            await db.fail_job(job["id"], error)
            return False
        return True

    def stop(self) -> None:
        self._running = False
        logger.info("Worker %s stopping", self.worker_id)

    @staticmethod
    async def enqueue(paper_id: Any, stage: str, priority: int = 0) -> Any:
        return await db.create_job(paper_id, stage, priority)
