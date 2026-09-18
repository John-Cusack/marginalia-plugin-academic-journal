"""The source-search provider returns SDK DTOs and degrades instead of raising."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from research_engine_sdk import (
    Availability,
    SourceMatch,
    SourceQuery,
    SourceSearchProvider,
)

from acad import source_search
from acad.infra import http_client
from acad.source_search import AcadSourceSearchProvider

WORKS = "https://api.openalex.org/works"


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(http_client.db, "log_api_call", AsyncMock())


def test_provider_satisfies_sdk_protocol():
    assert isinstance(AcadSourceSearchProvider(), SourceSearchProvider)
    assert AcadSourceSearchProvider().plugin_name == "acad"


@respx.mock
async def test_free_text_returns_sdk_matches_with_first_class_doi(openalex_works_response):
    respx.get(WORKS).mock(return_value=httpx.Response(200, json=openalex_works_response))

    matches = await AcadSourceSearchProvider().search(SourceQuery(query="attention"), limit=5)

    assert matches and all(isinstance(m, SourceMatch) for m in matches)
    first = matches[0]
    assert first.plugin == "acad"
    assert first.doi == "10.48550/arxiv.1706.03762"
    assert first.ingest_action.tool == "academic-journal.discover_by_doi"
    assert first.ingest_action.args == {"doi": first.doi}
    assert "corpus_source_pattern" not in first.metadata


@respx.mock
async def test_doi_query_short_circuits(openalex_works_response):
    work = openalex_works_response["results"][0]
    route = respx.get(f"{WORKS}/doi:10.48550/arxiv.1706.03762").mock(
        return_value=httpx.Response(200, json=work)
    )

    matches = await AcadSourceSearchProvider().search(
        SourceQuery(query="x", doi="https://doi.org/10.48550/ARXIV.1706.03762"), limit=5
    )

    assert route.called
    assert [m.confidence for m in matches] == [1.0]


@respx.mock
async def test_work_without_doi_ingests_by_title():
    respx.get(WORKS).mock(return_value=httpx.Response(200, json={"results": [
        {"id": "https://openalex.org/W1", "title": "No DOI Here", "doi": None}
    ]}))

    [match] = await AcadSourceSearchProvider().search(SourceQuery(query="q"), limit=5)

    assert match.doi is None
    assert match.ingest_action.tool == "academic-journal.discover_papers"
    assert match.availability is Availability.external_only


@respx.mock
async def test_provider_error_degrades_to_empty_and_logs(caplog):
    respx.get(WORKS).mock(return_value=httpx.Response(503))

    matches = await AcadSourceSearchProvider().search(SourceQuery(query="q"), limit=5)

    assert matches == []
    assert "source search failed" in caplog.text


@respx.mock
async def test_transport_error_degrades_to_empty():
    respx.get(WORKS).mock(side_effect=httpx.ConnectError("unreachable"))
    assert await AcadSourceSearchProvider().search(SourceQuery(query="q"), limit=5) == []


@respx.mock
async def test_failure_log_does_not_leak_contact_email(monkeypatch, caplog):
    monkeypatch.setenv("OPENALEX_EMAIL", "private.person@example.org")
    respx.get(WORKS).mock(side_effect=httpx.ConnectError(
        "failed https://api.openalex.org/works?mailto=private.person@example.org"
    ))

    await AcadSourceSearchProvider().search(SourceQuery(query="q"), limit=5)

    assert "private.person@example.org" not in caplog.text


@respx.mock
async def test_in_corpus_marking_is_best_effort(openalex_works_response, monkeypatch):
    # No RE_DB_URL: the corpus check fails, the matches still come back.
    respx.get(WORKS).mock(return_value=httpx.Response(200, json=openalex_works_response))
    matches = await AcadSourceSearchProvider().search(SourceQuery(query="q"), limit=5)
    assert matches[0].availability is not Availability.in_corpus


async def test_in_corpus_marks_ingested_doi(monkeypatch):
    match = source_search._build_match(
        {"id": "https://openalex.org/W9", "doi": "https://doi.org/10.1/ABC", "title": "T"}, 0.9
    )

    class Conn:
        async def fetch(self, sql, dois):
            assert dois == ["10.1/abc"]
            return [{"doi": "10.1/abc", "document_id": "doc-1"}]

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *exc):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    import acad.db.pool as pool_module

    monkeypatch.setattr(pool_module, "get_pool", AsyncMock(return_value=Pool()))
    await source_search._mark_in_corpus([match])

    assert match.availability is Availability.in_corpus
    assert match.document_id == "doc-1"
