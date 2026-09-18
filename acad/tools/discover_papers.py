"""academic-journal.discover_papers — Search academic APIs for papers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.pipeline.discovery import discover

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    query: str,
    max_papers: int = 200,
    sources: list[str] | None = None,
    *,
    context: PluginContext | None = None,
    **clients: Any,
) -> dict:
    config.bind_context(context)
    run_id = await discover(query, max_papers=max_papers, sources=sources)
    return {
        "discovery_run_id": str(run_id),
        "message": f"Discovery started for query: {query}",
    }
