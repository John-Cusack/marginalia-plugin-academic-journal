"""acad.pipeline_status — Show pipeline status."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from acad.db import queries as db
from acad.db.migrate import run_migrations
from acad.infra.worker import worker_status


@tool(
    id="acad.pipeline_status",
    description="Show the current state of the academic paper pipeline: "
                "papers by stage, job queue depths, recent errors, and API health.",
    input_schema={"type": "object", "properties": {}},
)
async def handler(**kwargs) -> dict:
    await run_migrations()
    status = await db.pipeline_status()
    status["workers"] = worker_status()
    return status
