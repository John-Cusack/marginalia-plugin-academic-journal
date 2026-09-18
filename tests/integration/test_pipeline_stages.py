"""Discovery, resolution, and acquisition against the database, with provider HTTP mocked."""

from __future__ import annotations

import httpx
import pytest
import respx

from acad import config
from acad.db import queries as db
from acad.pipeline.acquisition import acquire_handler
from acad.pipeline.discovery import discover, discover_by_doi
from acad.pipeline.resolution import resolve_handler

pytestmark = pytest.mark.integration

OA_WORKS = "https://api.openalex.org/works"
PDF_BYTES = b"%PDF-1.4\nsynthetic\n%%EOF\n"


@pytest.fixture(autouse=True)
def _bound(migrated, context):
    config.bind_context(context)


async def _jobs(stage: str) -> int:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM acad_jobs WHERE stage = $1 AND status = 'pending'", stage
        )


@respx.mock
async def test_discovery_stores_papers_identifiers_authors_and_queues_work(
    openalex_works_response,
):
    respx.get(OA_WORKS).mock(return_value=httpx.Response(200, json=openalex_works_response))

    run_id = await discover("attention", max_papers=2, sources=["openalex"])

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        papers = await conn.fetch("SELECT id, title, year, venue FROM acad_papers ORDER BY title")
        identifiers = await conn.fetch("SELECT source, external_id FROM acad_external_identifiers")
        authors = await conn.fetchval("SELECT COUNT(*) FROM acad_paper_authors")
        provenance = await conn.fetchrow(
            "SELECT source, query_text, discovery_run_id FROM acad_paper_provenance LIMIT 1"
        )
        run = await conn.fetchrow(
            "SELECT papers_found, papers_new, status FROM acad_discovery_runs WHERE id = $1", run_id
        )

    assert len(papers) == len(openalex_works_response["results"])
    assert {row["source"] for row in identifiers} == {"openalex", "doi"}
    assert authors > 0
    assert provenance["source"] == "openalex"
    assert provenance["discovery_run_id"] == run_id
    assert (run["papers_found"], run["papers_new"], run["status"]) == (2, 2, "completed")
    assert await _jobs("resolved") == len(papers)


@respx.mock
async def test_rediscovery_does_not_duplicate_papers(openalex_works_response):
    respx.get(OA_WORKS).mock(return_value=httpx.Response(200, json=openalex_works_response))

    await discover("attention", max_papers=2, sources=["openalex"])
    await discover("attention", max_papers=2, sources=["openalex"])

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM acad_papers") == 2


@respx.mock
async def test_discover_by_doi_returns_the_paper_id(openalex_works_response):
    work = openalex_works_response["results"][0]
    doi = work["doi"].replace("https://doi.org/", "")
    respx.get(f"{OA_WORKS}/doi:{doi}").mock(return_value=httpx.Response(200, json=work))

    paper_id = await discover_by_doi(doi)

    assert paper_id is not None
    assert (await db.get_paper(paper_id))["title"] == work["title"]
    # Already known: no second row, and no HTTP call needed.
    assert await discover_by_doi(doi) == paper_id


@respx.mock
async def test_unreachable_provider_completes_the_run_without_papers():
    respx.get(OA_WORKS).mock(side_effect=httpx.ConnectError("no route"))

    run_id = await discover("attention", max_papers=2, sources=["openalex"])

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        run = await conn.fetchrow(
            "SELECT papers_found, status FROM acad_discovery_runs WHERE id = $1", run_id
        )
        assert await conn.fetchval("SELECT COUNT(*) FROM acad_papers") == 0
    assert (run["papers_found"], run["status"]) == (0, "completed")


async def test_resolution_uses_the_arxiv_id_without_an_api_call():
    paper_id = await db.insert_paper({"title": "arXiv paper"})
    await db.insert_external_id(paper_id, "arxiv", "1706.03762")
    job_id = await db.create_job(paper_id, "resolved")

    await resolve_handler({"id": job_id, "paper_id": paper_id})

    paper = await db.get_paper(paper_id)
    assert paper["open_access_url"] == "https://arxiv.org/pdf/1706.03762.pdf"
    assert paper["pipeline_stage"] == "resolved"
    assert await _jobs("acquired") == 1


@respx.mock
async def test_unresolvable_paper_is_marked_not_failed():
    paper_id = await db.insert_paper({"title": "Paywalled"})
    await db.insert_external_id(paper_id, "doi", "10.9/paywalled")
    respx.get("https://api.unpaywall.org/v2/10.9/paywalled").mock(
        return_value=httpx.Response(200, json={"best_oa_location": None})
    )
    job_id = await db.create_job(paper_id, "resolved")

    await resolve_handler({"id": job_id, "paper_id": paper_id})

    paper = await db.get_paper(paper_id)
    assert paper["pipeline_stage"] == "unresolvable"
    assert paper["stage_status"] == "succeeded"
    assert await _jobs("acquired") == 0


@respx.mock
async def test_acquisition_downloads_into_the_plugin_data_directory(context):
    paper_id = await db.insert_paper(
        {"title": "arXiv paper", "open_access_url": "https://arxiv.org/pdf/1706.03762.pdf"}
    )
    await db.insert_external_id(paper_id, "arxiv", "1706.03762")
    respx.get("https://arxiv.org/pdf/1706.03762.pdf").mock(
        return_value=httpx.Response(200, content=PDF_BYTES)
    )
    job_id = await db.create_job(paper_id, "acquired")

    await acquire_handler({"id": job_id, "paper_id": paper_id})

    paper = await db.get_paper(paper_id)
    expected = context.data_dir / "papers" / f"{paper_id}.pdf"
    assert paper["file_path"] == str(expected)
    assert expected.read_bytes() == PDF_BYTES
    assert paper["pipeline_stage"] == "acquired"
    assert await _jobs("ingested") == 1


@respx.mock
async def test_a_download_that_is_not_a_pdf_is_rejected(context):
    paper_id = await db.insert_paper(
        {"title": "HTML paywall", "open_access_url": "https://arxiv.org/pdf/9999.99999.pdf"}
    )
    await db.insert_external_id(paper_id, "arxiv", "9999.99999")
    respx.get("https://arxiv.org/pdf/9999.99999.pdf").mock(
        return_value=httpx.Response(200, content=b"<html>login</html>")
    )

    with pytest.raises(ValueError, match="not a valid PDF"):
        await acquire_handler({"id": None, "paper_id": paper_id})

    assert not (context.data_dir / "papers" / f"{paper_id}.pdf").exists()
    assert (await db.get_paper(paper_id))["file_path"] is None


@respx.mock
async def test_api_calls_are_logged_with_credentials_removed(monkeypatch, openalex_works_response):
    monkeypatch.setenv("OPENALEX_EMAIL", "person@example.org")
    respx.get(OA_WORKS).mock(return_value=httpx.Response(200, json=openalex_works_response))

    await discover("attention", max_papers=2, sources=["openalex"])

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        logged = await conn.fetch("SELECT source, request_params::text AS params FROM acad_api_calls")
    assert logged
    assert all(row["source"] == "openalex" for row in logged)
    assert all("person@example.org" not in row["params"] for row in logged)
