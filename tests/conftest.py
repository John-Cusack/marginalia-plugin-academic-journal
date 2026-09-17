"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def openalex_works_response() -> dict:
    return json.loads((FIXTURES_DIR / "openalex_works_response.json").read_text())


@pytest.fixture
def s2_search_response() -> dict:
    return json.loads((FIXTURES_DIR / "s2_search_response.json").read_text())


@pytest.fixture
def crossref_works_response() -> dict:
    return json.loads((FIXTURES_DIR / "crossref_works_response.json").read_text())
