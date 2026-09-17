"""acad.import_manual_pdf — Import a manually downloaded PDF."""

from __future__ import annotations

import hashlib
from pathlib import Path

from research_engine.plugins.sdk import tool

from acad.db import queries as db
from acad.db.migrate import run_migrations
from acad.infra.job_queue import JobQueue


@tool(
    id="acad.import_manual_pdf",
    description="Link a manually downloaded PDF to an existing paper record, "
                "or create a new paper record and advance it to the 'acquired' stage.",
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the PDF file",
            },
            "paper_id": {
                "type": "string",
                "description": "UUID of an existing paper record",
            },
            "doi": {
                "type": "string",
                "description": "DOI to look up or create a paper record for",
            },
            "title": {
                "type": "string",
                "description": "Paper title (used if creating a new record)",
            },
        },
        "required": ["file_path"],
    },
)
async def handler(
    file_path: str,
    paper_id: str | None = None,
    doi: str | None = None,
    title: str | None = None,
    **kwargs,
) -> dict:
    await run_migrations()

    pdf_path = Path(file_path)
    if not pdf_path.exists():
        return {"error": f"File not found: {file_path}"}
    if not pdf_path.suffix.lower() == ".pdf":
        return {"error": "File must be a PDF"}

    content = pdf_path.read_bytes()
    if not content[:5] == b"%PDF-":
        return {"error": "File is not a valid PDF (missing %PDF- header)"}

    file_hash = hashlib.sha256(content).hexdigest()

    from uuid import UUID

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

    # Update paper with file info and advance to acquired
    await db.update_paper_file(pid, str(pdf_path.resolve()), file_hash)
    await db.update_paper_stage(pid, "acquired", "succeeded")

    # Enqueue ingestion
    await JobQueue.enqueue(pid, "ingested")

    return {
        "paper_id": str(pid),
        "file_hash": file_hash,
        "file_size": len(content),
        "status": "acquired",
        "message": "PDF imported and queued for ingestion",
    }
