"""SourceSearchProvider for the academic-journal plugin.

Maps a free-text/DOI/title query to ``SourceMatch`` records pulled from OpenAlex. Each
match carries an ``ingest_action`` naming the plugin's own discovery tools, which keeps
the ingestion path single-sourced and idempotent.

A provider failure — network, rate limit, open circuit, missing database — returns no
matches and logs why. ``search_sources`` fans out to every provider at once, and one
unreachable API must not cost the caller the others' results.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from research_engine_sdk import Availability, IngestAction, SourceMatch, SourceQuery

from acad import config
from acad.infra.http_client import ResilientHttpClient
from acad.pipeline.discovery import _reconstruct_abstract

logger = logging.getLogger(__name__)

OA_BASE = "https://api.openalex.org"

PROVIDER_NAME = "acad"
DISCOVER_BY_DOI_TOOL = "academic-journal.discover_by_doi"
DISCOVER_PAPERS_TOOL = "academic-journal.discover_papers"

_WORK_FIELDS = (
    "id,doi,title,abstract_inverted_index,publication_year,"
    "primary_location,cited_by_count,authorships,open_access,best_oa_location"
)


def _oa_params_base() -> dict[str, str]:
    email = os.environ.get("OPENALEX_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    return {"mailto": email} if email else {}


def _normalize_doi(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw.replace("https://doi.org/", "").lower().strip() or None


def _availability_for(work: dict[str, Any]) -> Availability:
    """Open-access status drives ingestability for academic papers."""
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    if oa.get("is_oa") or best.get("pdf_url") or best.get("landing_page_url"):
        return Availability.ingestable
    return Availability.external_only


def _build_match(work: dict[str, Any], confidence: float) -> SourceMatch | None:
    """Translate one OpenAlex work record into a SourceMatch."""
    oa_id_url = work.get("id") or ""
    oa_id = oa_id_url.rsplit("/", 1)[-1] if oa_id_url else ""
    if not oa_id:
        return None

    doi = _normalize_doi(work.get("doi"))
    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])
    ]
    authors = [a for a in authors if a]

    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    abstract = _reconstruct_abstract(work.get("abstract_inverted_index") or {})

    # Prefer DOI for the ingest action (more stable + idempotent across sources);
    # fall back to a query against the title.
    if doi:
        action = IngestAction(tool=DISCOVER_BY_DOI_TOOL, args={"doi": doi})
    else:
        action = IngestAction(
            tool=DISCOVER_PAPERS_TOOL,
            args={"query": work.get("title", ""), "max_papers": 1},
        )

    return SourceMatch(
        plugin=PROVIDER_NAME,
        source_id=oa_id,
        title=work.get("title") or "Untitled",
        authors=authors,
        year=work.get("publication_year"),
        # First-class so core deduplicates this paper against other providers.
        doi=doi,
        availability=_availability_for(work),
        confidence=confidence,
        ingest_action=action,
        metadata={
            "doi": doi,
            "venue": venue,
            "abstract": abstract or None,
            "openalex_id": oa_id,
            "citation_count": work.get("cited_by_count", 0),
        },
    )


async def _mark_in_corpus(matches: list[SourceMatch]) -> None:
    """Flag matches this plugin has already ingested. Best-effort.

    Core's own enrichment matches a document's exact source, which for this plugin is a
    PDF path under its data directory — not something a search result knows. The paper
    table does know: a DOI row with a ``document_id`` is in the corpus.
    """
    dois = [match.doi for match in matches if match.doi]
    if not dois:
        return
    try:
        from acad.db.pool import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT lower(ei.external_id) AS doi, p.document_id
                FROM acad_external_identifiers ei
                JOIN acad_papers p ON p.id = ei.paper_id
                WHERE ei.source = 'doi' AND lower(ei.external_id) = ANY($1::text[])
                  AND p.document_id IS NOT NULL""",
                dois,
            )
    except Exception as exc:
        logger.warning(
            "academic-journal source search skipped corpus check: %s",
            config.redact_text(str(exc)) or type(exc).__name__,
        )
        return
    ingested = {row["doi"]: str(row["document_id"]) for row in rows}
    for match in matches:
        if match.doi in ingested:
            match.availability = Availability.in_corpus
            match.document_id = ingested[match.doi]


class AcadSourceSearchProvider:
    """Cross-source provider for academic literature (OpenAlex primary)."""

    plugin_name: str = PROVIDER_NAME

    async def search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]:
        try:
            matches = await self._search(query, limit=limit)
        except Exception as exc:
            logger.warning(
                "academic-journal source search failed for %r: %s",
                query.doi or query.query,
                config.redact_text(str(exc)) or type(exc).__name__,
            )
            return []
        await _mark_in_corpus(matches)
        return matches

    async def _search(self, query: SourceQuery, *, limit: int) -> list[SourceMatch]:
        http = ResilientHttpClient()
        try:
            # 1. DOI hit short-circuits everything else.
            doi = _normalize_doi(query.doi)
            if doi:
                work = await http.get_json(
                    "openalex",
                    f"{OA_BASE}/works/doi:{doi}",
                    params=_oa_params_base(),
                )
                match = _build_match(work, confidence=1.0)
                return [match] if match else []

            # 2. Otherwise, build the most-precise free-text query we can.
            search_text = query.query
            if query.title and query.author:
                search_text = f"{query.title} {query.author}"
            elif query.title:
                search_text = query.title

            params = {
                **_oa_params_base(),
                "search": search_text,
                "per_page": str(max(1, min(limit, 25))),
                "select": _WORK_FIELDS,
            }
            if query.year:
                params["filter"] = f"publication_year:{query.year}"

            data = await http.get_json("openalex", f"{OA_BASE}/works", params=params)
            results = data.get("results", []) or []

            matches: list[SourceMatch] = []
            # OpenAlex results are already relevance-ranked; map rank to confidence.
            for rank, work in enumerate(results):
                confidence = max(0.1, 1.0 - rank * (1.0 / max(len(results), 1)))
                m = _build_match(work, confidence=confidence)
                if m:
                    matches.append(m)
            return matches
        finally:
            await http.close()
