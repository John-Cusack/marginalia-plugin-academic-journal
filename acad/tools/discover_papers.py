"""acad.discover_papers — Search academic APIs for papers."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from acad.db.migrate import run_migrations
from acad.pipeline.discovery import discover


@tool(
    id="acad.discover_papers",
    description="Search OpenAlex, Semantic Scholar, and/or Crossref for academic papers by query. "
                "Papers are inserted into the pipeline at the 'discovered' stage.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g., 'transformer attention mechanism')",
            },
            "max_papers": {
                "type": "integer",
                "description": "Maximum papers to discover per source (default 200)",
                "default": 200,
            },
            "sources": {
                "type": "array",
                "items": {"type": "string", "enum": ["openalex", "semantic_scholar", "crossref"]},
                "description": "Which sources to search (default: openalex + semantic_scholar if API key set)",
            },
        },
        "required": ["query"],
    },
)
async def handler(query: str, max_papers: int = 200, sources: list[str] | None = None, **kwargs) -> dict:
    await run_migrations()
    run_id = await discover(query, max_papers=max_papers, sources=sources)
    return {
        "discovery_run_id": str(run_id),
        "message": f"Discovery started for query: {query}",
    }
