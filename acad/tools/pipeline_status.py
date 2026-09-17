"""academic-journal.pipeline_status — Show pipeline status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.db import queries as db
from acad.infra.worker import worker_status

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(*, context: PluginContext | None = None, **clients: Any) -> dict:
    config.bind_context(context)
    status = await db.pipeline_status()
    status["workers"] = worker_status()
    return status
