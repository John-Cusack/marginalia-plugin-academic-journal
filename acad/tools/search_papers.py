"""academic-journal.search_papers — Search ingested academic papers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    query: str,
    year_min: int | None = None,
    year_max: int | None = None,
    venue: str | None = None,
    k: int = 20,
    *,
    corpus: Any = None,
    context: PluginContext | None = None,
    **clients: Any,
) -> dict:
    config.bind_context(context)
    if corpus is None:
        return {"error": "Corpus client not available"}

    # Scope to this plugin's papers through the registered 'academic_paper' filter
    # extension, which joins acad_papers.document_id. It is always applied, even with
    # no year/venue bounds: that join is what "academic paper" means here. A
    # document_type filter would match nothing — core types ingested PDFs by parser.
    extension: dict[str, Any] = {}
    if year_min is not None:
        extension["year_min"] = year_min
    if year_max is not None:
        extension["year_max"] = year_max
    if venue:
        extension["venue"] = venue

    filters: dict[str, Any] = {"extensions": {"academic_paper": extension}}
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
