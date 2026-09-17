"""acad.retry_failed — Re-enqueue failed pipeline jobs."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from acad.db import queries as db
from acad.db.migrate import run_migrations


@tool(
    id="acad.retry_failed",
    description="Re-enqueue failed pipeline jobs for retry. "
                "Optionally filter by stage and error pattern.",
    input_schema={
        "type": "object",
        "properties": {
            "stage": {
                "type": "string",
                "description": "Filter by pipeline stage (e.g., 'resolved', 'acquired')",
                "enum": ["resolved", "acquired", "ingested", "citations_extracted"],
            },
            "error_pattern": {
                "type": "string",
                "description": "Filter by error message substring",
            },
        },
    },
)
async def handler(
    stage: str | None = None,
    error_pattern: str | None = None,
    **kwargs,
) -> dict:
    await run_migrations()
    count = await db.retry_failed_jobs(stage=stage, error_pattern=error_pattern)
    return {
        "retried": count,
        "message": f"Re-enqueued {count} failed jobs",
    }
