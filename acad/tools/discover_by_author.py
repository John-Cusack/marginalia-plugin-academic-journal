"""acad.discover_by_author — Find papers by author."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from acad.db.migrate import run_migrations
from acad.pipeline.discovery import discover_by_author


@tool(
    id="acad.discover_by_author",
    description="Discover academic papers by author name or OpenAlex author ID.",
    input_schema={
        "type": "object",
        "properties": {
            "author_name": {
                "type": "string",
                "description": "Author name to search for",
            },
            "openalex_author_id": {
                "type": "string",
                "description": "OpenAlex author ID (e.g., 'A5023888391')",
            },
            "max_papers": {
                "type": "integer",
                "description": "Maximum papers to discover (default 200)",
                "default": 200,
            },
        },
    },
)
async def handler(
    author_name: str | None = None,
    openalex_author_id: str | None = None,
    max_papers: int = 200,
    **kwargs,
) -> dict:
    if not author_name and not openalex_author_id:
        return {"error": "Either author_name or openalex_author_id is required"}

    await run_migrations()
    run_id = await discover_by_author(
        author_name=author_name,
        openalex_author_id=openalex_author_id,
        max_papers=max_papers,
    )
    return {
        "discovery_run_id": str(run_id),
        "message": f"Author discovery started for: {author_name or openalex_author_id}",
    }
