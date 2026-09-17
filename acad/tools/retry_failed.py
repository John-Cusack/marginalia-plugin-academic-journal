"""academic-journal.retry_failed — Re-enqueue failed pipeline jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.db import queries as db

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    stage: str | None = None,
    error_pattern: str | None = None,
    *,
    context: PluginContext | None = None,
    **clients: Any,
) -> dict:
    config.bind_context(context)
    count = await db.retry_failed_jobs(stage=stage, error_pattern=error_pattern)
    return {
        "retried": count,
        "message": f"Re-enqueued {count} failed jobs",
    }
