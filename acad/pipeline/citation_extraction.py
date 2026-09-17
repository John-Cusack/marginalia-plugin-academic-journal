"""Citation extraction pipeline stage — parse bibliography, resolve cited works,
and build the corpus `cites` graph (with bounded recursive snowballing).

Flow per paper P (already ingested, has a core ``document_id``):
1. Flush any pending citations *targeting* P now that P has a document_id.
2. Extract bibliography entries from P's passages (LLM extraction).
3. For each cited work with a DOI: resolve it to a paper row (reusing the
   discovery pipeline, which cascades resolve→acquire→ingest→citations — this
   is the recursive acquire), then create a `cites` edge — immediately if the
   cited paper is already ingested, otherwise as a pending citation.

Recursion is bounded by ``MAX_CITATION_CRAWL_DEPTH`` (per-paper depth) and a
global snowball budget (``ACAD_MAX_SNOWBALL_PAPERS``). Every skip is logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from acad.db import queries as db
from acad.pipeline.discovery import discover, discover_by_doi

logger = logging.getLogger(__name__)

# Set by the plugin loader at startup (via acad.start_workers)
_extraction_client: Any = None
_corpus_client: Any = None
_edge_client: Any = None

MAX_CITATION_CRAWL_DEPTH = 2
_DEFAULT_SNOWBALL_BUDGET = 200


def set_clients(
    extraction: Any = None,
    corpus: Any = None,
    edge: Any = None,
) -> None:
    """Set the core clients (called during plugin initialization)."""
    global _extraction_client, _corpus_client, _edge_client
    if extraction:
        _extraction_client = extraction
    if corpus:
        _corpus_client = corpus
    if edge:
        _edge_client = edge


def _snowball_budget() -> int:
    try:
        return int(os.environ.get("ACAD_MAX_SNOWBALL_PAPERS", _DEFAULT_SNOWBALL_BUDGET))
    except ValueError:
        return _DEFAULT_SNOWBALL_BUDGET


def _entry_fields(record: dict) -> dict:
    """Bibliography entry data lives under 'fields' (or 'data', or the record)."""
    return record.get("fields") or record.get("data") or record


async def _create_cites_edge(
    citing_document_id: Any,
    cited_document_id: Any,
    *,
    attributes: dict,
    confidence: float,
    source_passage_id: Any = None,
) -> bool:
    """Create a document→document `cites` edge. Returns True on success."""
    if not _edge_client:
        return False
    try:
        await _edge_client.create({
            "source_kind": "document",
            "source_id": str(citing_document_id),
            "target_kind": "document",
            "target_id": str(cited_document_id),
            "relation_type": "cites",
            "attributes": attributes,
            "confidence": confidence,
            "source_passage_id": str(source_passage_id) if source_passage_id else None,
        })
        return True
    except Exception as exc:  # noqa: BLE001 — edges are best-effort
        logger.debug("Failed to create cites edge: %s", exc)
        return False


async def _flush_pending_to(cited_paper_id: Any, cited_document_id: Any) -> int:
    """Create edges for citations that were waiting on this paper to be ingested."""
    flushed = 0
    for pending in await db.get_pending_citations_for_cited(cited_paper_id):
        citing_doc = pending.get("citing_document_id")
        if not citing_doc:
            continue  # citing paper still not ingested; leave pending
        ok = await _create_cites_edge(
            citing_doc, cited_document_id,
            attributes=pending.get("attributes") or {},
            confidence=pending.get("confidence", 1.0),
        )
        if ok:
            await db.delete_pending_citation(pending["citing_paper_id"], cited_paper_id)
            flushed += 1
    return flushed


async def citation_extraction_handler(job: dict[str, Any]) -> None:
    """Extract citations from a paper's bibliography and build the cites graph."""
    paper = await db.get_paper(job["paper_id"])
    if not paper:
        raise ValueError(f"Paper {job['paper_id']} not found")

    paper_id = paper["id"]
    document_id = paper.get("document_id")

    if not document_id:
        logger.warning("Paper %s has no document_id, skipping citation extraction", paper_id)
        await db.update_paper_stage(paper_id, "complete", "succeeded")
        return

    if _extraction_client is None:
        logger.warning("Extraction client not configured, skipping citations")
        await db.update_paper_stage(paper_id, "complete", "succeeded")
        return

    try:
        # 1. Now that this paper is ingested, satisfy any citations that were
        #    waiting on it as a target.
        flushed = await _flush_pending_to(paper_id, document_id)
        if flushed:
            logger.info("Paper %s: flushed %d pending incoming citations", paper_id, flushed)

        # 2. Extract bibliography entries over the document's passages.
        result = await _extraction_client.extract(
            passage_ids=[],
            schema="bibliography_references:1",
            options={"document_id": str(document_id)},
        )
        records = result.get("records", [])
        logger.info("Paper %s: extracted %d bibliography entries", paper_id, len(records))

        # 3. Resolve cited works and build edges, with bounded recursion.
        depth = paper.get("crawl_depth") or 0
        budget = _snowball_budget()
        spawned = await db.count_snowballed_papers()

        resolved = 0
        for record in records:
            fields = _entry_fields(record)
            doi = (fields.get("doi") or "").replace("https://doi.org/", "").strip().lower()
            title = fields.get("title")
            confidence = float(fields.get("confidence", 1.0) or 1.0)
            source_passage_id = record.get("passage_id")
            attributes = {
                "via": "bibliography_references",
                "title": title,
                "doi": doi or None,
                "year": fields.get("year"),
            }

            can_spawn = depth < MAX_CITATION_CRAWL_DEPTH and spawned < budget

            cited: dict | None = None
            if doi:
                existing = await db.find_paper_by_external_id("doi", doi)
                if existing:
                    cited = existing
                elif can_spawn:
                    cited_id = await discover_by_doi(doi)
                    if cited_id:
                        await db.set_crawl_depth(cited_id, depth + 1)
                        cited = await db.get_paper(cited_id)
                        spawned += 1
                else:
                    logger.info(
                        "Paper %s: skipping snowball of DOI %s (depth=%d, spawned=%d/%d)",
                        paper_id, doi, depth, spawned, budget,
                    )
            elif title and can_spawn:
                # Title-only citation: seed discovery best-effort (no edge — we
                # can't reliably identify the resulting paper without a DOI).
                try:
                    await discover(query_text=title, max_papers=1)
                    spawned += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Title-only discovery failed for %r: %s", title, exc)
                continue

            if cited is None:
                continue

            if cited.get("document_id"):
                if await _create_cites_edge(
                    document_id, cited["document_id"],
                    attributes=attributes, confidence=confidence,
                    source_passage_id=source_passage_id,
                ):
                    resolved += 1
            else:
                # Target known but not ingested yet — edge created on its completion.
                await db.insert_pending_citation(
                    paper_id, cited["id"], attributes=attributes, confidence=confidence
                )

        logger.info(
            "Paper %s: created %d cites edges from %d entries", paper_id, resolved, len(records)
        )

    except Exception as exc:
        logger.error("Citation extraction failed for paper %s: %s", paper_id, exc)
        # Citations are best-effort — don't fail the whole pipeline.

    await db.update_paper_stage(paper_id, "complete", "succeeded")
