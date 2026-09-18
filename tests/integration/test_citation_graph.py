"""Citation graph building against real core `cites` edges.

Edges are created through core's own ``EdgeServiceAdapter`` — the same object the loader
injects as the plugin's ``edge`` client — so this exercises the public boundary rather
than core's repositories.
"""

from __future__ import annotations

from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

import acad.pipeline.citation_extraction as ce
from acad.db import queries as db

pytestmark = pytest.mark.integration


async def _count_cites(engine) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text("SELECT COUNT(*) FROM core.edges WHERE relation_type='cites'")
            )
        ).scalar()


@pytest.fixture
async def edge_client(engine, migrated):
    from research_engine.adapters.edge_client import EdgeServiceAdapter
    from research_engine.adapters.storage.postgres.engine import transaction
    from research_engine.adapters.storage.postgres.repositories.edges import PGEdgeRepo

    client = EdgeServiceAdapter(PGEdgeRepo(engine), partial(transaction, engine))
    yield client
    ce.set_clients(extraction=None, corpus=None, edge=None)
    ce._extraction_client = None
    ce._corpus_client = None
    ce._edge_client = None


def _extraction(records):
    client = AsyncMock()
    client.extract.return_value = {"records": records}
    return client


def _entry(doi, title="Cited work", confidence=0.95):
    return {"fields": {"doi": doi, "title": title, "confidence": confidence}, "passage_id": None}


async def test_edge_created_when_cited_already_ingested(engine, edge_client):
    citing = await db.insert_paper({"title": "Citing"})
    await db.update_paper_document_id(citing, uuid4())
    cited = await db.insert_paper({"title": "Cited"})
    await db.update_paper_document_id(cited, uuid4())
    await db.insert_external_id(cited, "doi", "10.1/known")
    ce.set_clients(extraction=_extraction([_entry("10.1/known")]), edge=edge_client)

    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 1

    # Re-running the stage must not duplicate the edge.
    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 1


async def test_pending_citation_flushes_when_the_target_is_ingested(engine, edge_client):
    citing = await db.insert_paper({"title": "Citing B"})
    await db.update_paper_document_id(citing, uuid4())
    cited = await db.insert_paper({"title": "Cited B"})  # not ingested yet
    await db.insert_external_id(cited, "doi", "10.2/pending")
    extraction = _extraction([_entry("10.2/pending", title="Cited B", confidence=0.9)])
    ce.set_clients(extraction=extraction, edge=edge_client)

    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 0
    assert len(await db.get_pending_citations_for_cited(cited)) == 1

    await db.update_paper_document_id(cited, uuid4())
    extraction.extract.return_value = {"records": []}
    await ce.citation_extraction_handler({"paper_id": cited})

    assert await _count_cites(engine) == 1
    assert await db.get_pending_citations_for_cited(cited) == []


async def test_depth_cap_blocks_snowballing(engine, edge_client, monkeypatch):
    deep = await db.insert_paper({"title": "Deep"})
    await db.update_paper_document_id(deep, uuid4())
    await db.set_crawl_depth(deep, ce.MAX_CITATION_CRAWL_DEPTH)
    discover = AsyncMock()
    monkeypatch.setattr(ce, "discover_by_doi", discover)
    ce.set_clients(extraction=_extraction([_entry("10.3/unknown")]), edge=edge_client)

    await ce.citation_extraction_handler({"paper_id": deep})

    discover.assert_not_called()
    assert await _count_cites(engine) == 0


async def test_snowball_budget_blocks_discovery(engine, edge_client, monkeypatch):
    paper = await db.insert_paper({"title": "Budget"})
    await db.update_paper_document_id(paper, uuid4())
    monkeypatch.setenv("ACAD_MAX_SNOWBALL_PAPERS", "0")
    discover = AsyncMock()
    monkeypatch.setattr(ce, "discover_by_doi", discover)
    ce.set_clients(extraction=_extraction([_entry("10.4/overbudget")]), edge=edge_client)

    await ce.citation_extraction_handler({"paper_id": paper})

    discover.assert_not_called()
    assert await _count_cites(engine) == 0


async def test_paper_completes_even_when_extraction_fails(engine, edge_client):
    paper = await db.insert_paper({"title": "Extraction fails"})
    await db.update_paper_document_id(paper, uuid4())
    extraction = AsyncMock()
    extraction.extract.side_effect = RuntimeError("LLM unavailable")
    ce.set_clients(extraction=extraction, edge=edge_client)

    await ce.citation_extraction_handler({"paper_id": paper})

    row = await db.get_paper(paper)
    assert (row["pipeline_stage"], row["stage_status"]) == ("complete", "succeeded")
