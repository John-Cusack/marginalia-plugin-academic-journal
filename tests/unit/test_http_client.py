"""ResilientHttpClient: host allowlist, best-effort audit logging, 429 handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from acad.infra import http_client
from acad.infra.http_client import HostNotAllowed, ResilientHttpClient, _retry_after


@pytest.fixture
def log_call(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(http_client.db, "log_api_call", mock)
    return mock


async def test_api_call_to_undeclared_host_is_refused(log_call):
    http = ResilientHttpClient()
    try:
        with pytest.raises(HostNotAllowed):
            await http.get_json("openalex", "https://evil.example.com/works")
    finally:
        await http.close()
    log_call.assert_not_called()


@respx.mock
async def test_logging_failure_does_not_fail_the_request(monkeypatch):
    monkeypatch.setattr(
        http_client.db, "log_api_call", AsyncMock(side_effect=RuntimeError("no table"))
    )
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    http = ResilientHttpClient()
    try:
        assert await http.get_json("openalex", "https://api.openalex.org/works") == {"results": []}
    finally:
        await http.close()


@respx.mock
async def test_non_numeric_retry_after_uses_default(log_call, monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(http_client.asyncio, "sleep", fake_sleep)
    route = respx.get("https://api.crossref.org/works")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
        httpx.Response(200, json={"message": {"items": []}}),
    ]
    http = ResilientHttpClient()
    try:
        await http.get_json("crossref", "https://api.crossref.org/works")
    finally:
        await http.close()
    assert sleeps == [2.0]


def test_retry_after_is_bounded():
    assert _retry_after("3600", 2.0) == 60.0
    assert _retry_after("-5", 2.0) == 0.0
    assert _retry_after(None, 4.0) == 4.0


async def test_download_refuses_non_http_scheme(log_call, tmp_path):
    http = ResilientHttpClient()
    try:
        with pytest.raises(ValueError, match="scheme"):
            await http.download("file:///etc/passwd", str(tmp_path / "x.pdf"))
    finally:
        await http.close()
