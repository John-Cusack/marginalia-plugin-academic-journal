"""OA URL resolution: waterfall strategy across multiple sources.

Order: existing URL → arXiv direct → Unpaywall → PubMed Central → CORE.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from acad.db import queries as db
from acad.infra.circuit_breaker import CircuitOpenError
from acad.infra.http_client import ResilientHttpClient
from acad.infra.job_queue import JobQueue

logger = logging.getLogger(__name__)


async def resolve_handler(job: dict[str, Any]) -> None:
    """Resolve a paper's OA URL via waterfall."""
    paper = await db.get_paper(job["paper_id"])
    if not paper:
        raise ValueError(f"Paper {job['paper_id']} not found")

    paper_id = paper["id"]

    # Fast path: already have an OA URL from discovery
    if paper.get("open_access_url"):
        await _mark_resolved(paper_id)
        return

    identifiers = await db.get_external_ids(paper_id)

    http = ResilientHttpClient()
    try:
        strategies = [
            _try_arxiv_direct,
            _try_unpaywall,
            _try_pubmed_central,
            _try_core,
        ]

        for strategy in strategies:
            try:
                url = await strategy(http, paper_id, identifiers, job.get("id"))
            except CircuitOpenError:
                continue
            if url:
                await db.update_paper_oa_url(paper_id, url)
                await _mark_resolved(paper_id)
                return
    finally:
        await http.close()

    # No OA URL available
    await db.update_paper_stage(
        paper_id, "unresolvable", "succeeded", "No OA URL found via any strategy"
    )


async def _try_arxiv_direct(
    http: ResilientHttpClient,
    paper_id: Any,
    identifiers: dict[str, str],
    job_id: Any,
) -> str | None:
    """Construct arXiv PDF URL directly — zero API calls needed."""
    arxiv_id = identifiers.get("arxiv")
    if not arxiv_id:
        return None

    arxiv_id = arxiv_id.removeprefix("arXiv:").removeprefix("arxiv:")
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    logger.info("arXiv direct URL for paper %s: %s", paper_id, pdf_url)
    return pdf_url


async def _try_unpaywall(
    http: ResilientHttpClient,
    paper_id: Any,
    identifiers: dict[str, str],
    job_id: Any,
) -> str | None:
    """Query Unpaywall for OA link using DOI."""
    doi = identifiers.get("doi")
    if not doi:
        return None

    email = os.environ.get("UNPAYWALL_EMAIL", "user@example.com")
    url = f"https://api.unpaywall.org/v2/{doi}"

    try:
        data = await http.get_json(
            "unpaywall", url,
            params={"email": email},
            paper_id=paper_id, job_id=job_id,
        )
        best_oa = data.get("best_oa_location") or {}
        return best_oa.get("url_for_pdf") or best_oa.get("url")
    except Exception as exc:
        logger.debug("Unpaywall failed for %s: %s", doi, exc)
        return None


async def _try_pubmed_central(
    http: ResilientHttpClient,
    paper_id: Any,
    identifiers: dict[str, str],
    job_id: Any,
) -> str | None:
    """Use NCBI elink to find PMC article."""
    pmid = identifiers.get("pmid")
    if not pmid:
        return None

    api_key = os.environ.get("NCBI_API_KEY", "")
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    params: dict[str, str] = {
        "dbfrom": "pubmed",
        "db": "pmc",
        "id": pmid,
        "retmode": "json",
    }
    if api_key:
        params["api_key"] = api_key

    try:
        data = await http.get_json(
            "ncbi", f"{base}/elink.fcgi",
            params=params,
            paper_id=paper_id, job_id=job_id,
        )
        linksets = data.get("linksets", [])
        if not linksets:
            return None
        linksetdbs = linksets[0].get("linksetdbs", [])
        pmc_linkset = next(
            (ls for ls in linksetdbs if ls.get("dbto") == "pmc"), None
        )
        if not pmc_linkset:
            return None
        links = pmc_linkset.get("links", [])
        if not links:
            return None

        pmc_id = links[0]
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}/pdf/"
    except Exception as exc:
        logger.debug("PMC lookup failed for PMID %s: %s", pmid, exc)
        return None


async def _try_core(
    http: ResilientHttpClient,
    paper_id: Any,
    identifiers: dict[str, str],
    job_id: Any,
) -> str | None:
    """Search CORE API v3 by DOI for a downloadable PDF URL."""
    api_key = os.environ.get("CORE_API_KEY", "")
    if not api_key:
        return None

    doi = identifiers.get("doi")
    if not doi:
        return None

    try:
        data = await http.get_json(
            "core",
            "https://api.core.ac.uk/v3/search/works",
            params={"q": f'doi:"{doi}"', "limit": "1"},
            headers={"Authorization": f"Bearer {api_key}"},
            paper_id=paper_id, job_id=job_id,
        )
        results = data.get("results", [])
        if not results:
            return None
        download_url = results[0].get("downloadUrl")
        return download_url if download_url else None
    except Exception as exc:
        logger.debug("CORE lookup failed for DOI %s: %s", doi, exc)
        return None


async def _mark_resolved(paper_id: Any) -> None:
    await db.update_paper_stage(paper_id, "resolved", "succeeded")
    await JobQueue.enqueue(paper_id, "acquired")
