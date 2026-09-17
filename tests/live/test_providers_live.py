"""Opt-in checks against the real provider APIs.

These cost network and are the only tests that can fail because a provider changed its
response shape. Run them deliberately:

    ACAD_LIVE_TESTS=1 pytest tests/live -q

Credentials are optional — every provider used here serves anonymous requests — and no
database is touched.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from research_engine_sdk import Availability, SourceMatch, SourceQuery

from acad.infra import http_client
from acad.source_search import AcadSourceSearchProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("ACAD_LIVE_TESTS") != "1",
        reason="live provider tests are opt-in: set ACAD_LIVE_TESTS=1",
    ),
]

ATTENTION_DOI = "10.48550/arxiv.1706.03762"


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """Live tests exercise providers, not persistence."""
    monkeypatch.setattr(http_client.db, "log_api_call", AsyncMock())
    monkeypatch.setattr(
        "acad.source_search._mark_in_corpus", AsyncMock(return_value=None)
    )


async def test_openalex_doi_lookup_still_maps_to_a_source_match():
    [match] = await AcadSourceSearchProvider().search(
        SourceQuery(query="attention", doi=ATTENTION_DOI), limit=1
    )

    assert isinstance(match, SourceMatch)
    assert match.doi == ATTENTION_DOI
    assert "Attention" in match.title
    assert match.authors
    assert match.year == 2017
    assert match.availability in set(Availability)
    assert match.ingest_action.tool == "academic-journal.discover_by_doi"


async def test_openalex_free_text_search_returns_ranked_matches():
    matches = await AcadSourceSearchProvider().search(
        SourceQuery(query="transformer attention mechanism"), limit=5
    )

    assert matches
    assert len(matches) <= 5
    assert matches == sorted(matches, key=lambda m: m.confidence, reverse=True)
    assert all(m.plugin == "acad" for m in matches)


async def test_unknown_doi_yields_no_match_rather_than_an_error():
    assert await AcadSourceSearchProvider().search(
        SourceQuery(query="x", doi="10.0000/definitely-not-a-real-doi"), limit=1
    ) == []
