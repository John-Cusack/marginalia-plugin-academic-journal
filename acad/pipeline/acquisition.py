"""Acquisition pipeline stage — downloads PDFs via module dispatch."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from acad import config
from acad.acquisition_modules import registry as acq_registry
from acad.db import queries as db
from acad.infra.job_queue import JobQueue
from acad.models import ExternalId, ExternalIdSource, Paper

logger = logging.getLogger(__name__)


async def acquire_handler(job: dict[str, Any]) -> None:
    """Download the PDF for a paper using the best available acquisition module."""
    paper_row = await db.get_paper(job["paper_id"])
    if not paper_row:
        raise ValueError(f"Paper {job['paper_id']} not found")

    paper_id = paper_row["id"]

    # Build Paper model with external IDs for module dispatch
    ext_ids_raw = await db.get_external_ids(paper_id)
    external_ids = []
    for source, ext_id in ext_ids_raw.items():
        with contextlib.suppress(ValueError):
            external_ids.append(ExternalId(source=ExternalIdSource(source), external_id=ext_id))

    paper = Paper(
        id=paper_id,
        title=paper_row["title"],
        abstract=paper_row.get("abstract"),
        year=paper_row.get("year"),
        venue=paper_row.get("venue"),
        citation_count=paper_row.get("citation_count", 0),
        influential_citation_count=paper_row.get("influential_citation_count", 0),
        open_access_url=paper_row.get("open_access_url"),
        external_ids=external_ids,
        pipeline_stage=paper_row["pipeline_stage"],
        stage_status=paper_row["stage_status"],
    )

    # Select best module
    result = await acq_registry.select_module(paper)
    if not result:
        raise ValueError(f"No acquisition module can handle paper {paper_id}")

    module, confidence, reason = result
    logger.info(
        "Paper %s: using %s (confidence=%.2f, reason=%s)",
        paper_id, module.id, confidence, reason,
    )

    # Downloads stay under the plugin data directory core assigned.
    dest = config.papers_dir() / f"{paper_id}.pdf"

    # Download
    acquired = await module.acquire(paper, dest)

    # Update paper record
    await db.update_paper_file(paper_id, acquired.file_path, acquired.file_hash)
    await db.update_paper_stage(paper_id, "acquired", "succeeded")

    # Enqueue ingestion
    await JobQueue.enqueue(paper_id, "ingested")
    logger.info(
        "Paper %s: acquired (%d bytes via %s)",
        paper_id, acquired.file_size, module.id,
    )
