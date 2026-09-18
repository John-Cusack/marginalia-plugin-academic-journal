"""academic-journal.start_workers — Launch in-process pipeline workers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from acad import config
from acad.infra.worker import start_workers, worker_status
from acad.pipeline.citation_extraction import set_clients
from acad.pipeline.ingestion import set_ingestion_client

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext


async def handler(
    *,
    context: PluginContext | None = None,
    ingestion: Any = None,
    corpus: Any = None,
    extraction: Any = None,
    edge: Any = None,
    **clients: Any,
) -> dict:
    # Bind the data directory and wire the core clients into the pipeline module
    # globals before launching (or re-confirming) workers. Idempotent — safe to call
    # repeatedly, and takes effect for already-running workers since they read the
    # module globals on every job. `edge` requires the `write` permission.
    config.bind_context(context)
    set_ingestion_client(ingestion)
    set_clients(extraction=extraction, corpus=corpus, edge=edge)

    started = await start_workers()
    status = worker_status()
    return {
        "workers_started": started,
        "status": status,
        "ingestion_client_wired": ingestion is not None,
        "citation_clients_wired": {
            "extraction": extraction is not None,
            "corpus": corpus is not None,
            "edge": edge is not None,
        },
        "message": (
            f"Started {started} pipeline workers; {status['active_workers']} running "
            "in this server process"
        ),
    }
