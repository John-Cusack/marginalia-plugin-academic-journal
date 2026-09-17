"""Unit tests for acquisition modules."""

from __future__ import annotations

import httpx
import pytest
import respx

from acad.acquisition_modules.arxiv import ArxivModule, _arxiv_id_from_url
from acad.acquisition_modules.direct_pdf import DirectPDFModule
from acad.acquisition_modules.registry import get_modules, select_module
from acad.acquisition_modules.unpaywall import UnpaywallModule
from acad.models import ExternalId, ExternalIdSource, Paper


@pytest.fixture
def arxiv_paper() -> Paper:
    return Paper(
        title="Test arXiv Paper",
        open_access_url="https://arxiv.org/pdf/1706.03762.pdf",
        external_ids=[
            ExternalId(source=ExternalIdSource.arxiv, external_id="1706.03762"),
            ExternalId(source=ExternalIdSource.doi, external_id="10.48550/arxiv.1706.03762"),
        ],
    )


@pytest.fixture
def doi_only_paper() -> Paper:
    return Paper(
        title="Test DOI Paper",
        external_ids=[
            ExternalId(source=ExternalIdSource.doi, external_id="10.1038/s41586-021-03819-2"),
        ],
    )


@pytest.fixture
def oa_url_paper() -> Paper:
    return Paper(
        title="Test OA Paper",
        open_access_url="https://example.com/paper.pdf",
    )


@pytest.fixture
def no_access_paper() -> Paper:
    return Paper(title="Paywalled Paper")


class TestArxivModule:
    @pytest.mark.asyncio
    async def test_high_confidence_for_arxiv(self, arxiv_paper):
        module = ArxivModule()
        confidence, reason = await module.can_acquire(arxiv_paper)
        assert confidence == 0.95
        assert "arXiv" in reason

    @pytest.mark.asyncio
    async def test_zero_confidence_for_non_arxiv(self, doi_only_paper):
        module = ArxivModule()
        confidence, _ = await module.can_acquire(doi_only_paper)
        assert confidence == 0.0


class TestDirectPDFModule:
    @pytest.mark.asyncio
    async def test_confidence_with_url(self, oa_url_paper):
        module = DirectPDFModule()
        confidence, _ = await module.can_acquire(oa_url_paper)
        assert confidence == 0.5

    @pytest.mark.asyncio
    async def test_zero_confidence_without_url(self, no_access_paper):
        module = DirectPDFModule()
        confidence, _ = await module.can_acquire(no_access_paper)
        assert confidence == 0.0


class TestUnpaywallModule:
    @pytest.mark.asyncio
    async def test_confidence_with_doi(self, doi_only_paper):
        module = UnpaywallModule()
        confidence, _ = await module.can_acquire(doi_only_paper)
        assert confidence == 0.7

    @pytest.mark.asyncio
    async def test_zero_confidence_without_doi(self, no_access_paper):
        module = UnpaywallModule()
        confidence, _ = await module.can_acquire(no_access_paper)
        assert confidence == 0.0


class TestModuleRegistry:
    @pytest.mark.asyncio
    async def test_selects_arxiv_for_arxiv_paper(self, arxiv_paper):
        result = await select_module(arxiv_paper)
        assert result is not None
        module, confidence, reason = result
        assert module.id == "arxiv"
        assert confidence == 0.95

    @pytest.mark.asyncio
    async def test_selects_unpaywall_for_doi_paper(self, doi_only_paper):
        result = await select_module(doi_only_paper)
        assert result is not None
        module, confidence, reason = result
        assert module.id == "unpaywall"

    @pytest.mark.asyncio
    async def test_returns_none_for_no_access(self, no_access_paper):
        result = await select_module(no_access_paper)
        assert result is None

    def test_modules_sorted_by_priority(self):
        modules = get_modules()
        priorities = [m.priority for m in modules]
        assert priorities == sorted(priorities, reverse=True)


class TestArxivIdRecovery:
    """can_acquire accepts an arXiv OA URL, so acquire must handle one."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),
            ("https://arxiv.org/pdf/2401.00001v2.pdf", "2401.00001v2"),
            ("https://arxiv.org/abs/hep-th/9901001", "hep-th/9901001"),
            ("http://ARXIV.ORG/pdf/2401.00002", "2401.00002"),
            ("https://example.org/paper.pdf", None),
            (None, None),
        ],
    )
    def test_identifier_is_recovered_from_the_url(self, url, expected):
        assert _arxiv_id_from_url(url) == expected

    @pytest.mark.asyncio
    async def test_acquire_uses_the_url_when_no_identifier_is_recorded(self, tmp_path):
        paper = Paper(title="arXiv only by URL", open_access_url="https://arxiv.org/pdf/2401.00001.pdf")
        module = ArxivModule()
        confidence, _ = await module.can_acquire(paper)
        assert confidence > 0

        with respx.mock:
            route = respx.get("https://arxiv.org/pdf/2401.00001.pdf").mock(
                return_value=httpx.Response(200, content=b"%PDF-1.4\n%%EOF\n")
            )
            acquired = await module.acquire(paper, tmp_path / "paper.pdf")

        assert route.called
        assert acquired.module_id == "arxiv"
        assert acquired.source_url == "https://arxiv.org/pdf/2401.00001.pdf"

    @pytest.mark.asyncio
    async def test_acquire_without_any_arxiv_evidence_explains_itself(self, tmp_path):
        with pytest.raises(ValueError, match="identifiers or open access URL"):
            await ArxivModule().acquire(Paper(title="No arXiv"), tmp_path / "x.pdf")
