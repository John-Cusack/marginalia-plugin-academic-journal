"""Academic paper discovery from OpenAlex (primary), Semantic Scholar, and Crossref."""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from acad.db import queries as db
from acad.infra.http_client import ResilientHttpClient
from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)

OA_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
CROSSREF_BASE = "https://api.crossref.org"

S2_FIELDS = (
    "paperId,externalIds,title,abstract,year,venue,"
    "citationCount,influentialCitationCount,isOpenAccess,openAccessPdf,authors"
)


def _oa_params_base() -> dict[str, str]:
    params: dict[str, str] = {}
    email = os.environ.get("OPENALEX_EMAIL") or os.environ.get("UNPAYWALL_EMAIL")
    if email:
        params["mailto"] = email
    return params


def _s2_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _reconstruct_abstract(inverted_index: dict[str, list[int]]) -> str:
    """Reconstruct abstract text from OpenAlex inverted index format."""
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(word for _, word in word_positions)


# --- OpenAlex ---


async def _discover_openalex(
    http: ResilientHttpClient,
    query_text: str,
    max_papers: int,
    run_id: UUID,
) -> tuple[int, int]:
    """Search OpenAlex. Returns (papers_found, papers_new)."""
    papers_found = 0
    papers_new = 0
    page = 1
    per_page = 50

    while papers_found < max_papers:
        remaining = max_papers - papers_found
        page_size = min(per_page, remaining)

        data = await http.get_json(
            "openalex",
            f"{OA_BASE}/works",
            params={
                **_oa_params_base(),
                "search": query_text,
                "per_page": str(page_size),
                "page": str(page),
                "select": "id,doi,title,abstract_inverted_index,publication_year,"
                          "primary_location,cited_by_count,authorships,"
                          "open_access,best_oa_location",
            },
        )

        results = data.get("results", [])
        if not results:
            break

        for rank, work in enumerate(results, start=(page - 1) * per_page + 1):
            papers_found += 1
            new = await _ingest_openalex_paper(work, run_id, query_text, rank)
            if new:
                papers_new += 1

        total = data.get("meta", {}).get("count", 0)
        if papers_found >= total or papers_found >= max_papers:
            break
        page += 1

    return papers_found, papers_new


async def _ingest_openalex_paper(
    work: dict, run_id: UUID, query_text: str, rank: int
) -> bool:
    """Ingest a single paper from OpenAlex. Returns True if new."""
    openalex_id = work.get("id", "")
    oa_short_id = openalex_id.rsplit("/", 1)[-1] if openalex_id else ""
    if not oa_short_id:
        return False

    # Dedup: OpenAlex ID
    existing = await db.find_paper_by_external_id("openalex", oa_short_id)
    if existing:
        return False

    # Dedup: DOI
    raw_doi = work.get("doi") or ""
    doi = raw_doi.replace("https://doi.org/", "") if raw_doi else ""
    if doi:
        existing = await db.find_paper_by_external_id("doi", doi)
        if existing:
            await db.insert_external_id(existing["id"], "openalex", oa_short_id)
            return False

    # Extract OA URL
    oa_info = work.get("open_access") or {}
    best_oa = work.get("best_oa_location") or {}
    oa_url = best_oa.get("pdf_url") or best_oa.get("landing_page_url")
    if not oa_url and oa_info.get("oa_url"):
        oa_url = oa_info["oa_url"]

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index") or {})

    primary_loc = work.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    venue = source.get("display_name", "")

    paper_id = await db.insert_paper({
        "title": work.get("title") or "Untitled",
        "abstract": abstract or None,
        "year": work.get("publication_year"),
        "venue": venue or None,
        "citation_count": work.get("cited_by_count", 0),
        "influential_citation_count": 0,
        "open_access_url": oa_url,
    })

    await db.insert_external_id(paper_id, "openalex", oa_short_id)
    if doi:
        await db.insert_external_id(paper_id, "doi", doi)

    authorships = work.get("authorships") or []
    author_records = []
    for i, a in enumerate(authorships):
        author_info = a.get("author") or {}
        oa_author_id = (author_info.get("id") or "").rsplit("/", 1)[-1] or None
        author_records.append({
            "name": author_info.get("display_name", "Unknown"),
            "position": i,
            "openalex_author_id": oa_author_id,
            "s2_author_id": None,
        })
    if author_records:
        await db.insert_paper_authors(paper_id, author_records)

    await db.insert_provenance({
        "paper_id": paper_id,
        "discovery_run_id": run_id,
        "source": "openalex",
        "query_text": query_text,
        "rank_in_results": rank,
        "raw_metadata": work,
    })

    await JobQueue.enqueue(paper_id, "resolved")
    return True


# --- Semantic Scholar ---


async def _discover_s2(
    http: ResilientHttpClient,
    query_text: str,
    max_papers: int,
    run_id: UUID,
) -> tuple[int, int]:
    """Search Semantic Scholar. Returns (papers_found, papers_new)."""
    papers_found = 0
    papers_new = 0
    offset = 0
    page_size = 100

    while offset < max_papers:
        limit = min(page_size, max_papers - offset)
        data = await http.get_json(
            "semantic_scholar",
            f"{S2_BASE}/paper/search",
            params={"query": query_text, "fields": S2_FIELDS, "offset": str(offset), "limit": str(limit)},
            headers=_s2_headers(),
        )

        results = data.get("data", [])
        if not results:
            break

        for rank, paper_data in enumerate(results, start=offset + 1):
            papers_found += 1
            new = await _ingest_s2_paper(paper_data, run_id, query_text, rank)
            if new:
                papers_new += 1

        total = data.get("total", 0)
        offset += len(results)
        if offset >= total:
            break

    return papers_found, papers_new


async def _ingest_s2_paper(
    paper_data: dict, run_id: UUID, query_text: str, rank: int
) -> bool:
    """Ingest a single paper from S2 search results. Returns True if new."""
    s2_id = paper_data.get("paperId")
    if not s2_id:
        return False

    existing = await db.find_paper_by_external_id("semantic_scholar", s2_id)
    if existing:
        return False

    external_ids = paper_data.get("externalIds") or {}
    doi = external_ids.get("DOI")
    if doi:
        existing = await db.find_paper_by_external_id("doi", doi)
        if existing:
            await db.insert_external_id(existing["id"], "semantic_scholar", s2_id)
            return False

    oa_pdf = paper_data.get("openAccessPdf") or {}
    oa_url = oa_pdf.get("url")

    paper_id = await db.insert_paper({
        "title": paper_data.get("title", "Untitled"),
        "abstract": paper_data.get("abstract"),
        "year": paper_data.get("year"),
        "venue": paper_data.get("venue"),
        "citation_count": paper_data.get("citationCount", 0),
        "influential_citation_count": paper_data.get("influentialCitationCount", 0),
        "open_access_url": oa_url,
    })

    await db.insert_external_id(paper_id, "semantic_scholar", s2_id)
    if doi:
        await db.insert_external_id(paper_id, "doi", doi)
    for source_key, id_key in [("arxiv", "ArXiv"), ("pmid", "PubMed")]:
        ext_id = external_ids.get(id_key)
        if ext_id:
            await db.insert_external_id(paper_id, source_key, str(ext_id))

    authors = paper_data.get("authors") or []
    author_records = [
        {
            "name": a.get("name", "Unknown"),
            "position": i,
            "openalex_author_id": None,
            "s2_author_id": a.get("authorId"),
        }
        for i, a in enumerate(authors)
    ]
    if author_records:
        await db.insert_paper_authors(paper_id, author_records)

    await db.insert_provenance({
        "paper_id": paper_id,
        "discovery_run_id": run_id,
        "source": "semantic_scholar",
        "query_text": query_text,
        "rank_in_results": rank,
        "raw_metadata": paper_data,
    })

    await JobQueue.enqueue(paper_id, "resolved")
    return True


# --- Crossref ---


async def _discover_crossref(
    http: ResilientHttpClient,
    query_text: str,
    max_papers: int,
    run_id: UUID,
) -> tuple[int, int]:
    """Search Crossref. Returns (papers_found, papers_new)."""
    papers_found = 0
    papers_new = 0
    offset = 0
    rows = 50

    while papers_found < max_papers:
        remaining = max_papers - papers_found
        page_rows = min(rows, remaining)

        data = await http.get_json(
            "crossref",
            f"{CROSSREF_BASE}/works",
            params={
                "query": query_text,
                "rows": str(page_rows),
                "offset": str(offset),
                "select": "DOI,title,abstract,published-print,container-title,"
                          "is-referenced-by-count,author,link",
            },
        )

        items = data.get("message", {}).get("items", [])
        if not items:
            break

        for rank, item in enumerate(items, start=offset + 1):
            papers_found += 1
            new = await _ingest_crossref_paper(item, run_id, query_text, rank)
            if new:
                papers_new += 1

        total = data.get("message", {}).get("total-results", 0)
        offset += len(items)
        if offset >= total or papers_found >= max_papers:
            break

    return papers_found, papers_new


async def _ingest_crossref_paper(
    item: dict, run_id: UUID, query_text: str, rank: int
) -> bool:
    """Ingest a single paper from Crossref. Returns True if new."""
    doi = item.get("DOI")
    if not doi:
        return False

    existing = await db.find_paper_by_external_id("doi", doi)
    if existing:
        return False

    titles = item.get("title", [])
    title = titles[0] if titles else "Untitled"

    published = item.get("published-print") or item.get("published-online") or {}
    date_parts = published.get("date-parts", [[]])
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    venues = item.get("container-title", [])
    venue = venues[0] if venues else None

    # Crossref sometimes includes OA links
    links = item.get("link", [])
    oa_url = None
    for link in links:
        if link.get("content-type") == "application/pdf":
            oa_url = link.get("URL")
            break

    paper_id = await db.insert_paper({
        "title": title,
        "abstract": item.get("abstract"),
        "year": year,
        "venue": venue,
        "citation_count": item.get("is-referenced-by-count", 0),
        "open_access_url": oa_url,
    })

    await db.insert_external_id(paper_id, "doi", doi)

    authors = item.get("author", [])
    author_records = [
        {
            "name": f"{a.get('given', '')} {a.get('family', '')}".strip() or "Unknown",
            "position": i,
            "openalex_author_id": None,
            "s2_author_id": None,
        }
        for i, a in enumerate(authors)
    ]
    if author_records:
        await db.insert_paper_authors(paper_id, author_records)

    await db.insert_provenance({
        "paper_id": paper_id,
        "discovery_run_id": run_id,
        "source": "crossref",
        "query_text": query_text,
        "rank_in_results": rank,
        "raw_metadata": item,
    })

    await JobQueue.enqueue(paper_id, "resolved")
    return True


# --- Discover by DOI ---


async def discover_by_doi(doi: str) -> UUID | None:
    """Look up a paper by DOI across OpenAlex and Crossref. Returns paper_id or None."""
    existing = await db.find_paper_by_external_id("doi", doi)
    if existing:
        return existing["id"]

    http = ResilientHttpClient()
    try:
        # Try OpenAlex first
        data = await http.get_json(
            "openalex",
            f"{OA_BASE}/works/doi:{doi}",
            params=_oa_params_base(),
        )
        run_id = await db.insert_discovery_run(f"doi:{doi}", source="openalex")
        new = await _ingest_openalex_paper(data, run_id, f"doi:{doi}", 1)
        await db.complete_discovery_run(run_id, 1, 1 if new else 0)

        result = await db.find_paper_by_external_id("doi", doi)
        return result["id"] if result else None
    except Exception:
        logger.warning("DOI lookup failed for %s", doi)
        return None
    finally:
        await http.close()


# --- Discover by Author ---


async def discover_by_author(
    author_name: str | None = None,
    openalex_author_id: str | None = None,
    max_papers: int = 200,
) -> UUID:
    """Discover papers by author. Returns discovery_run_id."""
    http = ResilientHttpClient()
    source_desc = openalex_author_id or author_name or "unknown"
    run_id = await db.insert_discovery_run(f"author:{source_desc}", source="openalex")
    total_found = 0
    total_new = 0

    try:
        if not openalex_author_id and author_name:
            # Search for author first
            data = await http.get_json(
                "openalex",
                f"{OA_BASE}/authors",
                params={**_oa_params_base(), "search": author_name, "per_page": "1"},
            )
            results = data.get("results", [])
            if results:
                openalex_author_id = results[0].get("id", "").rsplit("/", 1)[-1]

        if openalex_author_id:
            page = 1
            per_page = 50
            while total_found < max_papers:
                data = await http.get_json(
                    "openalex",
                    f"{OA_BASE}/works",
                    params={
                        **_oa_params_base(),
                        "filter": f"author.id:{openalex_author_id}",
                        "per_page": str(min(per_page, max_papers - total_found)),
                        "page": str(page),
                        "select": "id,doi,title,abstract_inverted_index,publication_year,"
                                  "primary_location,cited_by_count,authorships,"
                                  "open_access,best_oa_location",
                    },
                )
                results = data.get("results", [])
                if not results:
                    break

                for rank, work in enumerate(results, start=(page - 1) * per_page + 1):
                    total_found += 1
                    new = await _ingest_openalex_paper(work, run_id, f"author:{source_desc}", rank)
                    if new:
                        total_new += 1

                total = data.get("meta", {}).get("count", 0)
                if total_found >= total or total_found >= max_papers:
                    break
                page += 1

    except Exception as exc:
        logger.error("Author discovery failed: %s", exc)
        await db.complete_discovery_run(run_id, total_found, total_new, str(exc))
        raise
    finally:
        await http.close()

    await db.complete_discovery_run(run_id, total_found, total_new)
    return run_id


# --- Combined discovery ---


async def discover(
    query_text: str,
    max_papers: int = 200,
    sources: list[str] | None = None,
) -> UUID:
    """Discover papers from multiple sources.

    Returns discovery_run_id.
    """
    if sources is None:
        sources = ["openalex"]
        if os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
            sources.append("semantic_scholar")

    run_id = await db.insert_discovery_run(query_text, source=",".join(sources))
    total_found = 0
    total_new = 0

    http = ResilientHttpClient()
    try:
        for source in sources:
            try:
                if source == "openalex":
                    found, new = await _discover_openalex(http, query_text, max_papers, run_id)
                elif source == "semantic_scholar":
                    found, new = await _discover_s2(http, query_text, max_papers, run_id)
                elif source == "crossref":
                    found, new = await _discover_crossref(http, query_text, max_papers, run_id)
                else:
                    logger.warning("Unknown source: %s", source)
                    continue

                logger.info("Discovery [%s]: %d found, %d new", source, found, new)
                total_found += found
                total_new += new
            except Exception as exc:
                logger.error("Discovery [%s] failed: %s", source, exc)
    finally:
        await http.close()

    await db.complete_discovery_run(run_id, total_found, total_new)
    logger.info(
        "Discovery complete: %d found, %d new (across %d sources)",
        total_found, total_new, len(sources),
    )
    return run_id
