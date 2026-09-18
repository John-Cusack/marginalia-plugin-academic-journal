"""academic-journal.stop_workers — Stop the in-process pipeline workers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.infra.worker import stop_workers, worker_status

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(*, context: PluginContext | None = None, **clients: Any) -> dict:
    config.bind_context(context)
    stopped = await stop_workers()
    return {
        "workers_stopped": stopped,
        "status": worker_status(),
        "message": (
            f"Stopped {stopped} pipeline workers. A job interrupted mid-run stays "
            "in_progress until the job lease expires, then another worker reclaims it."
        ),
    }
