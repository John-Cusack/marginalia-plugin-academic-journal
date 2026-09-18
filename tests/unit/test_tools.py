"""Tool handlers: context binding, argument handling, and the client calls they make."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from research_engine_sdk import PluginContext

from acad import config
from acad.pipeline import ingestion
from acad.tools import discover_by_author, import_manual_pdf, search_papers

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
def context(tmp_path) -> PluginContext:
    return PluginContext(
        plugin_id="academic-journal",
        data_dir=tmp_path / "plugin-data" / "academic-journal",
        distribution_name="marginalia-ai-plugin-academic-journal",
        distribution_version="0.2.0",
    )


class _Corpus:
    def __init__(self):
        self.filters = None

    async def find_passages(self, query, filters=None, k=20):
        self.filters = filters

        class Result:
            hits = []
            total_candidates = 0

        return Result()


async def test_search_papers_scopes_through_the_filter_extension(context):
    corpus = _Corpus()

    await search_papers.handler("attention", venue="Nature", corpus=corpus, context=context)

    # A document_types filter would match nothing: core types ingested PDFs by parser.
    assert corpus.filters == {"extensions": {"academic_paper": {"venue": "Nature"}}}
    assert "document_types" not in corpus.filters


async def test_search_papers_applies_the_extension_even_unfiltered(context):
    corpus = _Corpus()
    await search_papers.handler("attention", corpus=corpus, context=context)
    assert corpus.filters == {"extensions": {"academic_paper": {}}}


async def test_search_papers_without_corpus_client(context):
    assert "error" in await search_papers.handler("q", corpus=None, context=context)


async def test_discover_by_author_requires_an_identifier(context):
    assert "error" in await discover_by_author.handler(context=context)


async def test_import_manual_pdf_requires_absolute_path(context):
    result = await import_manual_pdf.handler("relative/paper.pdf", context=context)
    assert "absolute" in result["error"]


async def test_import_manual_pdf_rejects_non_pdf(tmp_path, context):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    assert "must be a PDF" in (await import_manual_pdf.handler(str(path), context=context))["error"]


async def test_import_manual_pdf_rejects_bad_header(tmp_path, context):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"not a pdf")
    result = await import_manual_pdf.handler(str(path), context=context)
    assert "%PDF-" in result["error"]


async def test_import_manual_pdf_copies_into_plugin_data(tmp_path, context, monkeypatch):
    source = tmp_path / "downloads" / "paper.pdf"
    source.parent.mkdir()
    source.write_bytes(PDF_BYTES)
    paper_id = uuid4()
    monkeypatch.setattr(import_manual_pdf.db, "insert_paper", AsyncMock(return_value=paper_id))
    update_file = AsyncMock()
    monkeypatch.setattr(import_manual_pdf.db, "update_paper_file", update_file)
    monkeypatch.setattr(import_manual_pdf.db, "update_paper_stage", AsyncMock())
    monkeypatch.setattr(import_manual_pdf.JobQueue, "enqueue", AsyncMock())

    result = await import_manual_pdf.handler(str(source), title="A paper", context=context)

    stored = Path(result["file_path"])
    assert stored == context.data_dir / "papers" / f"{paper_id}.pdf"
    assert stored.read_bytes() == PDF_BYTES
    assert source.exists()  # the caller's copy is left alone
    assert update_file.await_args.args[1] == str(stored)


async def test_ingestion_dispatches_without_a_module_hint(tmp_path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(PDF_BYTES)
    paper_id = uuid4()
    client = AsyncMock()
    client.ingest_paths.return_value = {"ok": 1}
    client.find_existing.return_value = [{"document_id": str(uuid4())}]
    ingestion.set_ingestion_client(client)
    monkeypatch.setattr(
        ingestion.db, "get_paper",
        AsyncMock(return_value={"id": paper_id, "file_path": str(pdf)}),
    )
    monkeypatch.setattr(ingestion.db, "update_paper_document_id", AsyncMock())
    monkeypatch.setattr(ingestion.db, "update_paper_stage", AsyncMock())
    monkeypatch.setattr(ingestion.JobQueue, "enqueue", AsyncMock())

    await ingestion.ingestion_handler({"paper_id": paper_id})

    # "academic_journal" is not an ingestion module; passing it as a hint made core
    # raise "Unknown module hint" and every ingest fail.
    assert client.ingest_paths.await_args.kwargs.get("hint") is None
    assert len(client.ingest_paths.await_args.args[0]) == 1
    assert client.find_existing.await_args.kwargs == {"source": str(pdf.resolve())}


async def test_acquisition_downloads_into_plugin_data(context):
    config.bind_context(context)
    assert config.papers_dir() == context.data_dir / "papers"
