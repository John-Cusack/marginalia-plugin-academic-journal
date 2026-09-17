"""acad.start_workers — Launch background pipeline workers."""

from __future__ import annotations

from typing import Any

from research_engine.plugins.sdk import tool

from acad.db.migrate import run_migrations
from acad.infra.worker import start_workers, worker_status
from acad.pipeline.citation_extraction import set_clients
from acad.pipeline.ingestion import set_ingestion_client


@tool(
    id="acad.start_workers",
    description="Launch background pipeline workers that process papers through "
                "resolution → acquisition → ingestion → citation extraction. "
                "Workers poll for pending jobs and process them automatically.",
    input_schema={"type": "object", "properties": {}},
)
async def handler(
    *,
    ingestion: Any = None,
    corpus: Any = None,
    extraction: Any = None,
    edge: Any = None,
    **kwargs: Any,
) -> dict:
    # Wire the core clients into the pipeline module globals before launching
    # (or re-confirming) workers. Idempotent — safe to call repeatedly, and
    # takes effect for already-running workers since they read the module
    # globals on every job. `extraction`/`corpus`/`edge` power citation
    # extraction; `edge` requires the `write` permission in pack.yaml.
    set_ingestion_client(ingestion)
    set_clients(extraction=extraction, corpus=corpus, edge=edge)

    await run_migrations()
    count = await start_workers()
    return {
        "workers_started": count,
        "status": worker_status(),
        "ingestion_client_wired": ingestion is not None,
        "citation_clients_wired": {
            "extraction": extraction is not None,
            "corpus": corpus is not None,
            "edge": edge is not None,
        },
        "message": f"Started {count} pipeline workers",
    }
