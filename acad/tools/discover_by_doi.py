"""acad.discover_by_doi — Look up a single paper by DOI."""

from __future__ import annotations

from research_engine.plugins.sdk import tool

from acad.db.migrate import run_migrations
from acad.pipeline.discovery import discover_by_doi


@tool(
    id="acad.discover_by_doi",
    description="Look up a single academic paper by its DOI. "
                "If not already known, discovers it from OpenAlex.",
    input_schema={
        "type": "object",
        "properties": {
            "doi": {
                "type": "string",
                "description": "The DOI (e.g., '10.1038/s41586-021-03819-2')",
            },
        },
        "required": ["doi"],
    },
)
async def handler(doi: str, **kwargs) -> dict:
    await run_migrations()
    paper_id = await discover_by_doi(doi)
    if paper_id:
        return {"paper_id": str(paper_id), "doi": doi, "status": "found"}
    return {"paper_id": None, "doi": doi, "status": "not_found"}
