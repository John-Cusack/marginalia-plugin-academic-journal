#!/usr/bin/env python
"""End-to-end smoke test of an installed academic-journal distribution.

Run it inside a clean environment that has only the built wheel, the matching core
artifact, and the two test-only helpers it imports (``respx``, ``pymupdf``):

    RE_DB_URL=postgresql+asyncpg://user:pass@host:5432/acad_smoke_test \\
        python scripts/release_smoke.py

It proves, against a disposable database and with no provider credentials:

1. the installed distribution is discovered, enabled, and migrated to revision 2;
2. core loads every contribution;
3. fixture-backed discovery inserts papers through the plugin's own MCP tool;
4. a synthetic PDF is acquired into the plugin data directory and ingested by core;
5. citation extraction creates a real ``cites`` edge between the two documents;
6. everything it created is removed again.

Provider APIs and the embedding server are stubbed locally; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FAILURES: list[str] = []
CITING_DOI = "10.5555/smoke-citing"
CITED_DOI = "10.5555/smoke-cited"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIM = 1024


def check(condition: bool, label: str) -> None:
    print(f"{'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------------- stubs


class _Embeddings(BaseHTTPRequestHandler):
    """The smallest server that satisfies core's embedding wire contract."""

    def log_message(self, *args):  # noqa: A002 - silence the default access log
        pass

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send({
            "status": "ok",
            "model_name": EMBEDDING_MODEL,
            "model_version": "smoke",
            "dim": EMBEDDING_DIM,
            "device": "cpu",
            "warm": True,
            "concurrency": 1,
        })

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        texts = json.loads(self.rfile.read(length) or b"{}").get("texts", [])
        # Deterministic unit vectors: comparable with themselves, meaningless elsewhere.
        vectors = []
        for index, _text in enumerate(texts):
            vector = [0.0] * EMBEDDING_DIM
            vector[index % EMBEDDING_DIM] = 1.0
            vectors.append(vector)
        self._send({
            "embeddings": vectors,
            "model_name": EMBEDDING_MODEL,
            "model_version": "smoke",
            "dim": EMBEDDING_DIM,
        })


def start_embedding_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Embeddings)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def synthetic_pdf(path: Path, title: str, body: str) -> None:
    """A real PDF with real text, generated here — no third-party document involved."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 96), title, fontsize=16)
    for offset, line in enumerate(body.splitlines()):
        page.insert_text((72, 130 + offset * 14), line, fontsize=11)
    document.save(path)
    document.close()


def openalex_work(doi: str, title: str, pdf_url: str) -> dict:
    return {
        "id": f"https://openalex.org/W{abs(hash(doi)) % 10**8}",
        "doi": f"https://doi.org/{doi}",
        "title": title,
        "publication_year": 2024,
        "cited_by_count": 3,
        "abstract_inverted_index": {"Synthetic": [0], "fixture": [1]},
        "primary_location": {"source": {"display_name": "Journal of Smoke Tests"}},
        "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "A. Author"}}],
        "open_access": {"is_oa": True, "oa_url": pdf_url},
        "best_oa_location": {"pdf_url": pdf_url},
    }


# ----------------------------------------------------------------------- the smoke


def run_cli(*args: str) -> subprocess.CompletedProcess:
    binary = Path(sys.executable).parent / "research-engine"
    result = subprocess.run([str(binary), *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n")
    return result


def apply_core_schema(database_url: str) -> None:
    import research_engine

    ini = (
        Path(research_engine.__file__).parent
        / "adapters/storage/postgres/migrations/alembic.ini"
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        env={**os.environ, "RE_DB_URL": database_url},
        capture_output=True,
        text=True,
    )
    check(result.returncode == 0, "core schema applied to the disposable database")
    if result.returncode != 0:
        print(result.stdout, result.stderr, sep="\n")


async def main() -> int:
    database_url = os.environ.get("RE_DB_URL")
    if not database_url:
        print("RE_DB_URL is required and must name a disposable database.")
        return 2
    name = urlsplit(database_url).path.lstrip("/")
    if "test" not in name.lower() and "smoke" not in name.lower():
        print(f"refusing to run against database {name!r}: use a disposable one")
        return 2

    # No provider credentials, on purpose: nothing here may require them.
    for variable in (
        "SEMANTIC_SCHOLAR_API_KEY", "NCBI_API_KEY", "CORE_API_KEY",
        "OPENALEX_EMAIL", "UNPAYWALL_EMAIL",
    ):
        os.environ.pop(variable, None)

    server, embedding_url = start_embedding_server()
    # Core's data directory (and so the plugin's) goes somewhere disposable: this must
    # never write into a real ~/.research-engine.
    data_root = Path(tempfile.mkdtemp(prefix="acad-smoke-data-"))
    os.environ.update({
        "RE_DATA_DIR": str(data_root),
        "RE_EMBEDDING_PROVIDER": "remote_api",
        "RE_RERANKER_PROVIDER": "none",
        "RE_INFERENCE_BASE_URL": embedding_url,
        "RE_EMBEDDING_MODEL": EMBEDDING_MODEL,
        "RE_EMBEDDING_DIM": str(EMBEDDING_DIM),
    })

    import httpx
    import respx
    import sqlalchemy as sa
    from research_engine.composition import build_container
    from research_engine.config import load_settings
    from research_engine.mcp.dispatch import dispatch_tool

    import acad.pipeline.citation_extraction as citation_extraction
    from acad import config
    from acad.db import queries as db
    from acad.db.pool import close_pool
    from acad.pipeline.acquisition import acquire_handler
    from acad.pipeline.ingestion import ingestion_handler, set_ingestion_client
    from acad.pipeline.resolution import resolve_handler

    apply_core_schema(database_url)

    # 1. Discovery and approval through the installed CLI.
    listed = run_cli("plugin", "list")
    check("academic-journal" in listed.stdout, "plugin list shows academic-journal")
    audit = run_cli("plugin", "audit", "academic-journal")
    check("academic-journal.discover_papers" in audit.stdout, "audit lists the plugin's tools")
    check("revision 2" in audit.stdout, "audit declares database revision 2")
    check(run_cli("plugin", "enable", "academic-journal", "--yes").returncode == 0, "plugin enabled")
    migrated = run_cli("plugin", "migrate", "academic-journal", "--yes")
    check(migrated.returncode == 0, "plugin migrate succeeded")
    check('"current_revision": 2' in migrated.stdout, "migrate reached revision 2")

    # 2. Core loads every contribution.
    settings = load_settings()
    container = await build_container(settings)
    registry = container.registry
    tools = set(registry.get_mcp_tools())
    check(len([t for t in tools if t.startswith("academic-journal.")]) == 9, "9 tools registered")
    check("academic_paper" in registry.get_filter_extensions(), "filter extension registered")
    check("acad" in registry.get_source_search_providers(), "source provider registered")

    async with container.engine.connect() as conn:
        baseline_edges = (await conn.execute(
            sa.text("SELECT COUNT(*) FROM core.edges WHERE relation_type='cites'")
        )).scalar()
        baseline_documents = (await conn.execute(
            sa.text("SELECT COUNT(*) FROM core.documents")
        )).scalar()
        baseline_runs = {
            row[0] for row in
            await conn.execute(sa.text("SELECT id FROM core.ingestion_runs"))
        }

    citing_pdf = Path(os.environ.get("TMPDIR", "/tmp")) / "acad-smoke-citing.pdf"
    cited_pdf = Path(os.environ.get("TMPDIR", "/tmp")) / "acad-smoke-cited.pdf"
    synthetic_pdf(cited_pdf, "The Cited Work", "A synthetic open-access paper.\nIt is cited below.")
    synthetic_pdf(
        citing_pdf,
        "The Citing Work",
        "A synthetic open-access paper.\nReferences\n"
        f"A. Author. The Cited Work. doi:{CITED_DOI}",
    )

    paper_ids = {}
    try:
        with respx.mock(assert_all_called=False) as mock:
            # Provider APIs are stubbed; the local embedding server is not.
            mock.route(host="127.0.0.1").pass_through()
            mock.route(host="localhost").pass_through()
            for doi, title, pdf in (
                (CITING_DOI, "The Citing Work", "https://arxiv.org/pdf/2401.00001.pdf"),
                (CITED_DOI, "The Cited Work", "https://arxiv.org/pdf/2401.00002.pdf"),
            ):
                mock.get(f"https://api.openalex.org/works/doi:{doi}").mock(
                    return_value=httpx.Response(200, json=openalex_work(doi, title, pdf))
                )
            mock.get("https://arxiv.org/pdf/2401.00001.pdf").mock(
                return_value=httpx.Response(200, content=citing_pdf.read_bytes())
            )
            mock.get("https://arxiv.org/pdf/2401.00002.pdf").mock(
                return_value=httpx.Response(200, content=cited_pdf.read_bytes())
            )

            # 3. Fixture-backed discovery through the plugin's own MCP tool.
            for doi in (CITING_DOI, CITED_DOI):
                result = await dispatch_tool(
                    container, "academic-journal.discover_by_doi", {"doi": doi}
                )
                check(result.get("status") == "found", f"discovered {doi}")
                paper_ids[doi] = result["paper_id"]

            clients = container.plugin_loader.build_plugin_clients("academic-journal")
            config.bind_context(clients["context"])
            data_dir = Path(clients["context"].data_dir)
            set_ingestion_client(clients["ingestion"])

            # 4. Resolve and acquire into the plugin data directory.
            for doi, paper_id in paper_ids.items():
                await resolve_handler({"id": None, "paper_id": paper_id})
                await acquire_handler({"id": None, "paper_id": paper_id})
                stored = Path((await db.get_paper(paper_id))["file_path"])
                check(
                    stored.parent == data_dir / "papers" and stored.is_file(),
                    f"acquired {doi} into the plugin data directory",
                )

            # 5. Ingest both PDFs through core.
            for doi, paper_id in paper_ids.items():
                await ingestion_handler({"id": None, "paper_id": paper_id})
                paper = await db.get_paper(paper_id)
                check(paper["document_id"] is not None, f"ingested {doi} and linked its document")

        # 6. Citation extraction: bibliography records come from a fixture, edges are real.
        class FixtureExtraction:
            async def extract(self, passage_ids, schema, options=None):
                return {"records": [{
                    "fields": {"doi": CITED_DOI, "title": "The Cited Work", "confidence": 0.95},
                    "passage_id": None,
                }]}

        citation_extraction.set_clients(
            extraction=FixtureExtraction(), corpus=clients["corpus"], edge=clients["edge"]
        )
        await citation_extraction.citation_extraction_handler(
            {"id": None, "paper_id": paper_ids[CITING_DOI]}
        )

        citing = await db.get_paper(paper_ids[CITING_DOI])
        cited = await db.get_paper(paper_ids[CITED_DOI])
        async with container.engine.connect() as conn:
            edges = (
                await conn.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM core.edges WHERE relation_type='cites' "
                        "AND source_id = :src AND target_id = :tgt"
                    ),
                    {"src": citing["document_id"], "tgt": cited["document_id"]},
                )
            ).scalar()
        check(edges == 1, "a cites edge links the citing document to the cited one")
        check(citing["pipeline_stage"] == "complete", "the citing paper reached 'complete'")

        # 7. The plugin's search tool finds the ingested papers.
        found = await dispatch_tool(
            container, "academic-journal.search_papers", {"query": "synthetic", "k": 5}
        )
        check(found.get("count", 0) > 0, "search_papers returns ingested passages")

    finally:
        # 8. Exact cleanup: remove what this run created, leave the rest alone.
        document_ids = []
        for paper_id in paper_ids.values():
            paper = await db.get_paper(paper_id)
            if paper and paper["document_id"]:
                document_ids.append(paper["document_id"])
        async with container.engine.begin() as conn:
            for document_id in document_ids:
                await conn.execute(
                    sa.text(
                        "DELETE FROM core.edges WHERE source_id = :id OR target_id = :id"
                    ),
                    {"id": document_id},
                )
                # Ingestion audit rows reference the document.
                await conn.execute(
                    sa.text("DELETE FROM core.ingestion_items WHERE document_id = :id"),
                    {"id": document_id},
                )
                await conn.execute(
                    sa.text("DELETE FROM core.documents WHERE id = :id"), {"id": document_id}
                )
            if baseline_runs:
                await conn.execute(
                    sa.text("DELETE FROM core.ingestion_runs WHERE id != ALL(:keep)"),
                    {"keep": list(baseline_runs)},
                )
            else:
                await conn.execute(sa.text("DELETE FROM core.ingestion_runs"))
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            for table in (
                "acad_pending_citations", "acad_api_calls", "acad_paper_provenance",
                "acad_discovery_runs", "acad_external_identifiers", "acad_paper_authors",
                "acad_jobs", "acad_papers",
            ):
                await conn.execute(f"DELETE FROM {table}")
        async with container.engine.connect() as conn:
            remaining_edges = (await conn.execute(
                sa.text("SELECT COUNT(*) FROM core.edges WHERE relation_type='cites'")
            )).scalar()
            remaining_documents = (await conn.execute(
                sa.text("SELECT COUNT(*) FROM core.documents")
            )).scalar()
            remaining_runs = {
                row[0] for row in
                await conn.execute(sa.text("SELECT id FROM core.ingestion_runs"))
            }
        check(
            (remaining_edges, remaining_documents, remaining_runs)
            == (baseline_edges, baseline_documents, baseline_runs),
            "cleanup left the corpus exactly as it was found",
        )

        shutil.rmtree(data_root, ignore_errors=True)
        check(not data_root.exists(), "plugin data directory removed")
        for path in (citing_pdf, cited_pdf):
            path.unlink(missing_ok=True)
        await close_pool()
        await container.close()
        server.shutdown()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("release smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
