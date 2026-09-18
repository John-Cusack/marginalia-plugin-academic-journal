"""The academic_paper filter extension, composed the way core composes it.

Core does ``passages.c.id.in_(extension.build_clause(value))``; these run that against a
real schema, with real rows, so a clause that compiles but selects nothing is caught.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from research_engine.adapters.storage.postgres.schema import passages
from research_engine.domain.filter_extension import FilterExtension
from research_engine.testing import Corpus

from acad.db import queries as db
from acad.filters import AcademicPaperFilter

pytestmark = pytest.mark.integration


@pytest.fixture
async def corpus(engine, migrated):
    helper = Corpus(engine)
    yield helper
    await helper.cleanup()


async def _paper_with_passage(corpus, *, text: str, **paper):
    document_id = await corpus.add_document(title=paper.get("title", "paper"))
    passage_id = await corpus.add_passage(document_id, text)
    paper_id = await db.insert_paper({"title": "T", **paper})
    await db.update_paper_document_id(paper_id, document_id)
    return passage_id


async def _select(engine, value) -> set:
    clause = AcademicPaperFilter().build_clause(value)
    async with engine.connect() as conn:
        rows = await conn.execute(sa.select(passages.c.id).where(passages.c.id.in_(clause)))
        return {row[0] for row in rows}


async def test_filter_satisfies_the_core_protocol():
    assert isinstance(AcademicPaperFilter(), FilterExtension)


async def test_year_range_selects_only_matching_papers(engine, corpus):
    old = await _paper_with_passage(corpus, text="old work", year=1999)
    new = await _paper_with_passage(corpus, text="new work", year=2021)

    assert await _select(engine, {"year_min": 2010}) == {new}
    assert await _select(engine, {"year_max": 2000}) == {old}
    assert await _select(engine, {"year_min": 1990, "year_max": 2030}) == {old, new}


async def test_venue_is_a_case_insensitive_substring(engine, corpus):
    nature = await _paper_with_passage(corpus, text="a", venue="Nature Methods")
    await _paper_with_passage(corpus, text="b", venue="Cell")

    assert await _select(engine, {"venue": "nature"}) == {nature}


async def test_citation_counts_and_open_access(engine, corpus):
    cited = await _paper_with_passage(corpus, text="c", citation_count=500)
    await _paper_with_passage(corpus, text="d", citation_count=1)
    open_access = await _paper_with_passage(
        corpus, text="e", open_access_url="https://example.org/a.pdf"
    )

    assert await _select(engine, {"min_citations": 100}) == {cited}
    assert await _select(engine, {"open_access": True}) == {open_access}


async def test_empty_filter_still_scopes_to_academic_papers(engine, corpus):
    """search_papers relies on this: no bounds means "papers", not "everything"."""
    paper_passage = await _paper_with_passage(corpus, text="a paper")
    other_document = await corpus.add_document(title="not a paper")
    other_passage = await corpus.add_passage(other_document, "unrelated")

    selected = await _select(engine, {})

    assert paper_passage in selected
    assert other_passage not in selected


async def test_values_are_bound_not_interpolated(engine, corpus):
    await _paper_with_passage(corpus, text="a", venue="Nature")

    assert await _select(engine, {"venue": "'; DROP TABLE acad_papers; --"}) == set()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('acad_papers') IS NOT NULL")
