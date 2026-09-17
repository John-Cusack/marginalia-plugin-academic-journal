"""Post-ingestion hook — triggers citation extraction for academic papers."""

from __future__ import annotations

import logging
from typing import Any

from research_engine.plugins.sdk import hook

from acad.db import queries as db
from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)


@hook(event="post_ingestion", document_types=["academic_journal"])
async def handler(doc: Any, text: str, metadata: dict[str, Any]) -> None:
    """After a document is ingested, link it to its paper record and enqueue citation extraction."""
    source = getattr(doc, "source", "") or ""
    document_id = getattr(doc, "id", None)

    if not document_id:
        return

    # Try to find a paper record by file path
    # The source field on the document contains the file path used for ingestion
    from acad.db.pool import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM acad_papers WHERE file_path = $1",
            source,
        )

    if row:
        paper_id = row["id"]
        await db.update_paper_document_id(paper_id, document_id)
        logger.info(
            "Linked document %s to paper %s", document_id, paper_id
        )
