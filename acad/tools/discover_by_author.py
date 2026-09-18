"""academic-journal.discover_by_author — Find papers by author."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.pipeline.discovery import discover_by_author

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    author_name: str | None = None,
    openalex_author_id: str | None = None,
    max_papers: int = 200,
    *,
    context: PluginContext | None = None,
    **clients: Any,
) -> dict:
    if not author_name and not openalex_author_id:
        return {"error": "Either author_name or openalex_author_id is required"}

    config.bind_context(context)
    run_id = await discover_by_author(
        author_name=author_name,
        openalex_author_id=openalex_author_id,
        max_papers=max_papers,
    )
    return {
        "discovery_run_id": str(run_id),
        "message": f"Author discovery started for: {author_name or openalex_author_id}",
    }
