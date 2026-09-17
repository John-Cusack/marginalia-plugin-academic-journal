"""Ingestion pipeline stage — bridge to core IngestionOrchestrator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from acad.db import queries as db
from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)

# Set by the plugin loader at startup
_ingestion_client: Any = None


def set_ingestion_client(client: Any) -> None:
    """Set the core ingestion client (called during plugin initialization)."""
    global _ingestion_client
    _ingestion_client = client


async def ingestion_handler(job: dict[str, Any]) -> None:
    """Ingest a paper's PDF into the core Corpus Engine."""
    if _ingestion_client is None:
        raise RuntimeError("Ingestion client not configured — was the plugin loaded with ingest permission?")

    paper = await db.get_paper(job["paper_id"])
    if not paper:
        raise ValueError(f"Paper {job['paper_id']} not found")

    paper_id = paper["id"]
    file_path = paper.get("file_path")
    if not file_path:
        raise ValueError(f"Paper {paper_id} has no file_path")

    pdf_path = Path(file_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found at {file_path}")

    # Call core ingestion
    stats = await _ingestion_client.ingest_paths([pdf_path], hint="academic_journal")

    if stats.get("ok", 0) > 0 or stats.get("skipped", 0) > 0:
        # The core orchestrator doesn't return document IDs directly, so look
        # the document up by its source path and persist the link. Citation
        # extraction needs document_id to run, so this is load-bearing.
        await _link_document(paper_id, pdf_path)
        await db.update_paper_stage(paper_id, "ingested", "succeeded")
        await JobQueue.enqueue(paper_id, "citations_extracted")
        verb = "ingested into core" if stats.get("ok", 0) > 0 else "already ingested (dedup)"
        logger.info("Paper %s: %s", paper_id, verb)
    else:
        raise RuntimeError(
            f"Core ingestion failed for paper {paper_id}: {stats}"
        )


async def _link_document(paper_id: Any, pdf_path: Path) -> None:
    """Resolve and persist the core document_id for an ingested paper."""
    try:
        existing = await _ingestion_client.find_existing(source_pattern=str(pdf_path))
        if existing:
            await db.update_paper_document_id(paper_id, existing[0]["document_id"])
            logger.info("Paper %s: linked to document %s", paper_id, existing[0]["document_id"])
        else:
            logger.warning("Paper %s: could not resolve document_id for %s", paper_id, pdf_path)
    except Exception as exc:  # noqa: BLE001 — linking is best-effort
        logger.warning("Paper %s: document linking failed: %s", paper_id, exc)
