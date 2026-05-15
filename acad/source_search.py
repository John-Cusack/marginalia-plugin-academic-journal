"""SourceSearchProvider for the academic-journal plugin.

Maps a free-text/DOI/title query to ``SourceMatch`` records pulled from
OpenAlex (and Crossref as a fallback when DOI is supplied). Each match is
returned with an ``ingest_action`` that calls the existing
``acad.discover_by_doi`` / ``acad.discover_papers`` MCP tools — keeps the
ingestion path single-sourced and idempotent.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from research_engine.domain.source_search import (
    Availability,
    IngestAction,
    SourceMatch,
    SourceQuery,
)

from acad.infra.http_client import ResilientHttpClient
from acad.pipeline.discovery import _reconstruct_abstract

logger = logging.getLogger(__name__)

OA_BASE = "https://api.openalex.org"
CROSSREF_BASE = "https://api.crossref.org"


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
        action = IngestAction(tool="acad_discover_by_doi", args={"doi": doi})
    else:
        action = IngestAction(
            tool="acad_discover_papers",
            args={"query": work.get("title", ""), "max_papers": 1},
        )

    return SourceMatch(
        plugin="acad",
        source_id=oa_id,
        title=work.get("title") or "Untitled",
        authors=authors,
        year=work.get("publication_year"),
        availability=_availability_for(work),
        confidence=confidence,
        ingest_action=action,
        metadata={
            "doi": doi,
            "venue": venue,
            "abstract": abstract or None,
            "openalex_id": oa_id,
            "citation_count": work.get("cited_by_count", 0),
            # Pattern for IngestionOrchestrator.find_existing — academic plugin's
            # ingestion writes the DOI URL into the document's source field.
            "corpus_source_pattern": f"doi.org/{doi}" if doi else None,
        },
    )


class AcadSourceSearchProvider:
    """Cross-source provider for academic literature (OpenAlex primary)."""

    plugin_name: str = "acad"

    async def search(
        self, query: SourceQuery, *, limit: int
    ) -> list[SourceMatch]:
        http = ResilientHttpClient()
        try:
            # 1. DOI hit short-circuits everything else.
            doi = _normalize_doi(query.doi)
            if doi:
                try:
                    work = await http.get_json(
                        "openalex",
                        f"{OA_BASE}/works/doi:{doi}",
                        params=_oa_params_base(),
                    )
                    match = _build_match(work, confidence=1.0)
                    return [match] if match else []
                except Exception as e:
                    logger.warning("openalex doi lookup failed: %s", e)
                    return []

            # 2. Otherwise, build the most-precise free-text query we can.
            search_text = query.query
            if query.title and query.author:
                search_text = f"{query.title} {query.author}"
            elif query.title:
                search_text = query.title

            params = {
                **_oa_params_base(),
                "search": search_text,
                "per_page": str(min(limit, 25)),
                "select": (
                    "id,doi,title,abstract_inverted_index,publication_year,"
                    "primary_location,cited_by_count,authorships,"
                    "open_access,best_oa_location"
                ),
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
