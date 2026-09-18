"""Unit tests run with no database, no network, and no core."""

from __future__ import annotations

import pytest

from acad import config
from acad.infra import circuit_breaker, rate_limiter


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # An exported RE_DB_URL usually points at the real corpus. Removing it makes any
    # accidental database access fail with PluginConfigError instead of writing there.
    monkeypatch.delenv(config.DATABASE_URL_ENV, raising=False)
    for name in config.SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config.reset()
    circuit_breaker.reset_all()
    rate_limiter.reset_all()
    yield
    config.reset()
    circuit_breaker.reset_all()
    rate_limiter.reset_all()
