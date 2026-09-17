"""Integration tests for the citation graph (requires PostgreSQL + research_engine).

Run from the core env so research_engine is importable, with RE_DB_URL pointing at a
disposable database that has the core schema applied — never the research corpus, since
the fixture deletes every `cites` edge and all acad_* rows:
    PYTHONPATH=<this-repo> RE_DB_URL=postgresql+asyncpg://<user>:<pass>@<host>:<port>/<disposable_db> \
      uv run --group dev pytest <this-repo>/tests/integration/test_citation_graph.py

Skipped automatically when RE_DB_URL is unset or research_engine is absent.
"""

from __future__ import annotations

import importlib.util
import os
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("RE_DB_URL") or importlib.util.find_spec("research_engine") is None,
    reason="Requires PostgreSQL (RE_DB_URL) and research_engine on the path",
)


def _db_url() -> str:
    return os.environ["RE_DB_URL"]


async def _count_cites(engine) -> int:
    import sqlalchemy as sa
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(
            "SELECT COUNT(*) FROM core.edges WHERE relation_type='cites'"))).scalar()


@pytest.fixture
async def ctx():
    """Engine + real EdgeServiceAdapter + clean acad/edge tables."""
    import sqlalchemy as sa
    from research_engine.adapters.storage.postgres.engine import build_engine, transaction
    from research_engine.adapters.storage.postgres.repositories.edges import PGEdgeRepo
    from research_engine.adapters.edge_client import EdgeServiceAdapter

    import acad.pipeline.citation_extraction as ce
    from acad.db.migrate import run_migrations
    from acad.db.pool import close_pool

    await run_migrations()
    engine = await build_engine(_db_url())

    async def reset():
        async with engine.begin() as conn:
            for tbl in ("core.edges WHERE relation_type='cites'", "acad_pending_citations",
                        "acad_external_identifiers", "acad_jobs", "acad_papers"):
                await conn.execute(sa.text(f"DELETE FROM {tbl}"))

    await reset()
    edge = EdgeServiceAdapter(PGEdgeRepo(engine), partial(transaction, engine))
    yield {"engine": engine, "edge": edge, "ce": ce}
    await reset()
    await close_pool()
    await engine.dispose()


async def test_edge_created_when_cited_ingested(ctx):
    from acad.db import queries as db
    ce, engine = ctx["ce"], ctx["engine"]

    citing = await db.insert_paper({"title": "Citing"})
    await db.update_paper_document_id(citing, uuid4())
    cited = await db.insert_paper({"title": "Cited"})
    await db.update_paper_document_id(cited, uuid4())
    await db.insert_external_id(cited, "doi", "10.1/known")

    extraction = AsyncMock()
    extraction.extract.return_value = {"records": [
        {"data": {"doi": "10.1/known", "title": "Cited", "confidence": 0.95}, "passage_id": None}]}
    ce.set_clients(extraction=extraction, corpus=None, edge=ctx["edge"])

    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 1
    # idempotent
    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 1


async def test_pending_then_flush_on_completion(ctx):
    from acad.db import queries as db
    ce, engine = ctx["ce"], ctx["engine"]

    citing = await db.insert_paper({"title": "Citing B"})
    await db.update_paper_document_id(citing, uuid4())
    cited = await db.insert_paper({"title": "Cited B"})  # not ingested yet
    await db.insert_external_id(cited, "doi", "10.2/pending")

    extraction = AsyncMock()
    extraction.extract.return_value = {"records": [
        {"data": {"doi": "10.2/pending", "title": "Cited B", "confidence": 0.9}, "passage_id": None}]}
    ce.set_clients(extraction=extraction, corpus=None, edge=ctx["edge"])

    await ce.citation_extraction_handler({"paper_id": citing})
    assert await _count_cites(engine) == 0
    assert len(await db.get_pending_citations_for_cited(cited)) == 1

    # cited gets ingested and runs its stage -> pending flushes into an edge
    await db.update_paper_document_id(cited, uuid4())
    extraction.extract.return_value = {"records": []}
    await ce.citation_extraction_handler({"paper_id": cited})
    assert await _count_cites(engine) == 1
    assert len(await db.get_pending_citations_for_cited(cited)) == 0


async def test_depth_cap_blocks_snowball(ctx, monkeypatch):
    from acad.db import queries as db
    ce, engine = ctx["ce"], ctx["engine"]

    deep = await db.insert_paper({"title": "Deep"})
    await db.update_paper_document_id(deep, uuid4())
    await db.set_crawl_depth(deep, ce.MAX_CITATION_CRAWL_DEPTH)

    discover_mock = AsyncMock()
    monkeypatch.setattr(ce, "discover_by_doi", discover_mock)
    extraction = AsyncMock()
    extraction.extract.return_value = {"records": [
        {"data": {"doi": "10.3/unknown", "title": "Way down", "confidence": 0.8}, "passage_id": None}]}
    ce.set_clients(extraction=extraction, corpus=None, edge=ctx["edge"])

    await ce.citation_extraction_handler({"paper_id": deep})
    discover_mock.assert_not_called()
    assert await _count_cites(engine) == 0
