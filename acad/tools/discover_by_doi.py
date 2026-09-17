"""academic-journal.discover_by_doi — Look up a single paper by DOI."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.pipeline.discovery import discover_by_doi

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    doi: str, *, context: PluginContext | None = None, **clients: Any
) -> dict:
    config.bind_context(context)
    paper_id = await discover_by_doi(doi)
    if paper_id:
        return {"paper_id": str(paper_id), "doi": doi, "status": "found"}
    return {"paper_id": None, "doi": doi, "status": "not_found"}
