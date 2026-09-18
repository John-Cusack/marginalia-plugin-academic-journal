"""Unit tests for resolution strategies."""

from __future__ import annotations

import pytest


class TestArxivDirect:
    """Test arXiv URL construction (no HTTP needed)."""

    @pytest.mark.asyncio
    async def test_constructs_pdf_url(self):
        from acad.pipeline.resolution import _try_arxiv_direct

        url = await _try_arxiv_direct(
            None, None, {"arxiv": "1706.03762"}, None
        )
        assert url == "https://arxiv.org/pdf/1706.03762.pdf"

    @pytest.mark.asyncio
    async def test_strips_prefix(self):
        from acad.pipeline.resolution import _try_arxiv_direct

        url = await _try_arxiv_direct(
            None, None, {"arxiv": "arXiv:2301.12345"}, None
        )
        assert url == "https://arxiv.org/pdf/2301.12345.pdf"

    @pytest.mark.asyncio
    async def test_returns_none_without_arxiv_id(self):
        from acad.pipeline.resolution import _try_arxiv_direct

        url = await _try_arxiv_direct(None, None, {"doi": "10.1234/test"}, None)
        assert url is None
