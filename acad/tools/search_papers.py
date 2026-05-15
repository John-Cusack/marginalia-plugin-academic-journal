"""acad.search_papers — Search ingested academic papers."""

from __future__ import annotations

from typing import Any

from research_engine.plugins.sdk import tool

from acad.db.migrate import run_migrations


@tool(
    id="acad.search_papers",
    description="Search across ingested academic papers using vector+keyword hybrid search. "
                "Only searches papers that have been fully ingested into the corpus.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "year_min": {
                "type": "integer",
                "description": "Minimum publication year filter",
            },
            "year_max": {
                "type": "integer",
                "description": "Maximum publication year filter",
            },
            "venue": {
                "type": "string",
                "description": "Filter by venue/journal name (substring match)",
            },
            "k": {
                "type": "integer",
                "description": "Number of results (default 20)",
                "default": 20,
            },
        },
        "required": ["query"],
    },
)
async def handler(
    query: str,
    year_min: int | None = None,
    year_max: int | None = None,
    venue: str | None = None,
    k: int = 20,
    corpus: Any = None,
    **kwargs: Any,
) -> dict:
    await run_migrations()

    if corpus is None:
        return {"error": "Corpus client not available"}

    # Year/venue filtering routes through the registered 'academic_paper'
    # FilterExtension (acad/filters.py), which joins the academic_paper SQL
    # table — far more selective than JSONB containment against passage metadata.
    extensions: dict[str, Any] = {}
    if year_min is not None:
        extensions["year_min"] = year_min
    if year_max is not None:
        extensions["year_max"] = year_max
    if venue:
        extensions["venue"] = venue

    filters: dict[str, Any] = {"document_types": ["academic_journal"]}
    if extensions:
        filters["extensions"] = {"academic_paper": extensions}

    result = await corpus.find_passages(query, filters=filters, k=k)

    return {
        "query": query,
        "count": len(result.hits),
        "total_candidates": result.total_candidates,
        "results": [
            {
                "passage_id": str(h.passage_id),
                "document_id": str(h.document_id),
                "score": h.score,
                "text": h.text,
                "metadata": h.metadata,
                "locator": h.locator,
            }
            for h in result.hits
        ],
    }
