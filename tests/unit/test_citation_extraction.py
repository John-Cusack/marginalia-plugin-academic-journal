"""Unit tests for the citation extraction stage (graph building + snowballing).

The handler reads module-global clients and calls module-level db functions;
we patch both so the decision logic (edge vs pending, depth/budget gating,
pending flush) can be exercised without a database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import acad.pipeline.citation_extraction as ce


@pytest.fixture
def patched_db(monkeypatch):
    """Patch every db.* function the handler touches with AsyncMocks."""
    fns = {
        "get_paper": AsyncMock(),
        "update_paper_stage": AsyncMock(),
        "find_paper_by_external_id": AsyncMock(return_value=None),
        "set_crawl_depth": AsyncMock(),
        "count_snowballed_papers": AsyncMock(return_value=0),
        "insert_pending_citation": AsyncMock(),
        "get_pending_citations_for_cited": AsyncMock(return_value=[]),
        "delete_pending_citation": AsyncMock(),
    }
    for name, mock in fns.items():
        monkeypatch.setattr(ce.db, name, mock)
    return fns


@pytest.fixture
def clients():
    """Wire mock extraction/corpus/edge clients; reset module globals after."""
    extraction = AsyncMock()
    corpus = AsyncMock()
    edge_client = AsyncMock()
    edge_client.create = AsyncMock(return_value={"id": str(uuid4())})
    ce.set_clients(extraction=extraction, corpus=corpus, edge=edge_client)
    yield {"extraction": extraction, "corpus": corpus, "edge": edge_client}
    ce._extraction_client = None
    ce._corpus_client = None
    ce._edge_client = None


def _bib_record(doi=None, title="Some Title", confidence=0.9, year=2020):
    return {"data": {"doi": doi, "title": title, "confidence": confidence, "year": year},
            "passage_id": str(uuid4())}


async def test_creates_edge_when_cited_already_ingested(patched_db, clients):
    paper_id, doc_id, cited_doc = uuid4(), uuid4(), uuid4()
    patched_db["get_paper"].return_value = {"id": paper_id, "document_id": doc_id, "crawl_depth": 0}
    patched_db["find_paper_by_external_id"].return_value = {"id": uuid4(), "document_id": cited_doc}
    clients["extraction"].extract.return_value = {"records": [_bib_record(doi="10.1/abc")]}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    clients["edge"].create.assert_awaited_once()
    edge_arg = clients["edge"].create.await_args.args[0]
    assert edge_arg["relation_type"] == "cites"
    assert edge_arg["target_id"] == str(cited_doc)
    patched_db["insert_pending_citation"].assert_not_called()
    patched_db["update_paper_stage"].assert_awaited_with(paper_id, "complete", "succeeded")


async def test_pending_when_cited_not_ingested(patched_db, clients, monkeypatch):
    paper_id, doc_id, cited_id = uuid4(), uuid4(), uuid4()
    patched_db["get_paper"].side_effect = [
        {"id": paper_id, "document_id": doc_id, "crawl_depth": 0},  # the paper being processed
        {"id": cited_id, "document_id": None, "crawl_depth": 1},     # the newly snowballed paper
    ]
    discover_mock = AsyncMock(return_value=cited_id)
    monkeypatch.setattr(ce, "discover_by_doi", discover_mock)
    clients["extraction"].extract.return_value = {"records": [_bib_record(doi="10.1/xyz")]}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    discover_mock.assert_awaited_once_with("10.1/xyz")
    patched_db["set_crawl_depth"].assert_awaited_once_with(cited_id, 1)
    patched_db["insert_pending_citation"].assert_awaited_once()
    clients["edge"].create.assert_not_called()


async def test_depth_cap_blocks_snowball(patched_db, clients, monkeypatch):
    paper_id, doc_id = uuid4(), uuid4()
    # At max depth: unknown DOI must NOT trigger discovery.
    patched_db["get_paper"].return_value = {
        "id": paper_id, "document_id": doc_id, "crawl_depth": ce.MAX_CITATION_CRAWL_DEPTH,
    }
    discover_mock = AsyncMock()
    monkeypatch.setattr(ce, "discover_by_doi", discover_mock)
    clients["extraction"].extract.return_value = {"records": [_bib_record(doi="10.1/deep")]}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    discover_mock.assert_not_called()
    clients["edge"].create.assert_not_called()


async def test_budget_cap_blocks_snowball(patched_db, clients, monkeypatch):
    paper_id, doc_id = uuid4(), uuid4()
    patched_db["get_paper"].return_value = {"id": paper_id, "document_id": doc_id, "crawl_depth": 0}
    patched_db["count_snowballed_papers"].return_value = 999  # over budget
    monkeypatch.setenv("ACAD_MAX_SNOWBALL_PAPERS", "10")
    discover_mock = AsyncMock()
    monkeypatch.setattr(ce, "discover_by_doi", discover_mock)
    clients["extraction"].extract.return_value = {"records": [_bib_record(doi="10.1/overbudget")]}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    discover_mock.assert_not_called()


async def test_flushes_pending_incoming_on_completion(patched_db, clients):
    paper_id, doc_id, citing_doc, citing_paper = uuid4(), uuid4(), uuid4(), uuid4()
    patched_db["get_paper"].return_value = {"id": paper_id, "document_id": doc_id, "crawl_depth": 1}
    patched_db["get_pending_citations_for_cited"].return_value = [
        {"citing_paper_id": citing_paper, "cited_paper_id": paper_id,
         "citing_document_id": citing_doc, "attributes": {}, "confidence": 1.0},
    ]
    clients["extraction"].extract.return_value = {"records": []}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    clients["edge"].create.assert_awaited_once()
    edge_arg = clients["edge"].create.await_args.args[0]
    assert edge_arg["source_id"] == str(citing_doc)
    assert edge_arg["target_id"] == str(doc_id)
    patched_db["delete_pending_citation"].assert_awaited_once_with(citing_paper, paper_id)


async def test_skips_when_no_document_id(patched_db, clients):
    paper_id = uuid4()
    patched_db["get_paper"].return_value = {"id": paper_id, "document_id": None}

    await ce.citation_extraction_handler({"paper_id": paper_id})

    clients["extraction"].extract.assert_not_called()
    patched_db["update_paper_stage"].assert_awaited_with(paper_id, "complete", "succeeded")
