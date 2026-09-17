"""academic-journal.import_manual_pdf — Import a manually downloaded PDF."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from acad import config
from acad.db import queries as db
from acad.infra.job_queue import JobQueue

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    file_path: str,
    paper_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
    *,
    context: PluginContext | None = None,
    **clients: Any,
) -> dict:
    config.bind_context(context)

    # The caller names this file explicitly; it is read once and copied into the
    # plugin data directory, so later stages never depend on where it came from.
    pdf_path = Path(file_path).expanduser()
    if not pdf_path.is_absolute():
        return {"error": "file_path must be absolute; the server's working directory is not yours"}
    if not pdf_path.is_file():
        return {"error": f"File not found: {file_path}"}
    if pdf_path.suffix.lower() != ".pdf":
        return {"error": "File must be a PDF"}

    content = pdf_path.read_bytes()
    if content[:5] != b"%PDF-":
        return {"error": "File is not a valid PDF (missing %PDF- header)"}

    file_hash = hashlib.sha256(content).hexdigest()

    # Find or create paper record
    if paper_id:
        pid = UUID(paper_id)
        paper = await db.get_paper(pid)
        if not paper:
            return {"error": f"Paper {paper_id} not found"}
    elif doi:
        paper = await db.find_paper_by_external_id("doi", doi)
        if paper:
            pid = paper["id"]
        else:
            pid = await db.insert_paper({
                "title": title or f"Paper DOI:{doi}",
                "pipeline_stage": "discovered",
                "stage_status": "pending",
            })
            await db.insert_external_id(pid, "doi", doi)
    elif title:
        pid = await db.insert_paper({
            "title": title,
            "pipeline_stage": "discovered",
            "stage_status": "pending",
        })
    else:
        return {"error": "At least one of paper_id, doi, or title is required"}

    dest = config.papers_dir() / f"{pid}.pdf"
    if pdf_path.resolve() != dest.resolve():
        shutil.copyfile(pdf_path, dest)

    # Update paper with file info and advance to acquired
    await db.update_paper_file(pid, str(dest.resolve()), file_hash)
    await db.update_paper_stage(pid, "acquired", "succeeded")

    # Enqueue ingestion
    await JobQueue.enqueue(pid, "ingested")

    return {
        "paper_id": str(pid),
        "file_path": str(dest.resolve()),
        "file_hash": file_hash,
        "file_size": len(content),
        "status": "acquired",
        "message": "PDF imported and queued for ingestion",
    }
