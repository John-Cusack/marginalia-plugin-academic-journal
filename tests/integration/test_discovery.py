"""Integration tests for discovery pipeline (mocked HTTP)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def oa_response() -> dict:
    return json.loads((FIXTURES_DIR / "openalex_works_response.json").read_text())


@pytest.fixture
def s2_response() -> dict:
    return json.loads((FIXTURES_DIR / "s2_search_response.json").read_text())


@pytest.fixture
def crossref_response() -> dict:
    return json.loads((FIXTURES_DIR / "crossref_works_response.json").read_text())


class TestAbstractReconstruction:
    def test_inverted_index(self):
        from acad.pipeline.discovery import _reconstruct_abstract

        inverted = {
            "The": [0, 9],
            "dominant": [1],
            "sequence": [2, 10],
            "transduction": [3, 11],
            "models": [4, 12],
            "are": [5],
            "based": [6],
            "on": [7],
            "complex": [8],
        }
        result = _reconstruct_abstract(inverted)
        assert result.startswith("The dominant sequence transduction models are based on complex")

    def test_empty_inverted_index(self):
        from acad.pipeline.discovery import _reconstruct_abstract

        assert _reconstruct_abstract({}) == ""
        assert _reconstruct_abstract(None) == ""
