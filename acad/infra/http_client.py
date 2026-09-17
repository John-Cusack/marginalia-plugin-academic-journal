"""Resilient HTTP client wrapper with rate limiting and circuit breaking."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from acad import config
from acad.db import queries as db
from acad.infra.circuit_breaker import get_breaker
from acad.infra.rate_limiter import get_limiter

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_MAX_RETRY_AFTER = 60.0

#: Metadata APIs this plugin calls. Must equal ``network_allowlist`` in plugin.yaml
#: (a contract test holds them together). PDF downloads are not limited to these hosts:
#: open-access copies live on publisher and repository sites, which is why the
#: manifest declares ``network: full``.
API_HOSTS = frozenset({
    "api.openalex.org",
    "api.semanticscholar.org",
    "api.crossref.org",
    "api.unpaywall.org",
    "arxiv.org",
    "api.core.ac.uk",
    "eutils.ncbi.nlm.nih.gov",
    "www.ncbi.nlm.nih.gov",
})


class HostNotAllowed(ValueError):
    """A metadata API call targeted a host outside the declared allowlist."""


def _retry_after(value: str | None, default: float) -> float:
    try:
        seconds = float(value) if value is not None else default
    except ValueError:  # an HTTP-date; not worth parsing for a bounded wait
        seconds = default
    return max(0.0, min(seconds, _MAX_RETRY_AFTER))


async def _log_call(record: dict[str, Any]) -> None:
    """Record an API call. Best-effort: auditing must never fail the request itself."""
    try:
        await db.log_api_call(record)
    except Exception as exc:
        logger.warning(
            "academic-journal could not log API call to %s: %s",
            record.get("source"),
            config.redact_text(str(exc)) or type(exc).__name__,
        )


class ResilientHttpClient:
    """HTTP client with per-source rate limiting, circuit breaking, and API call logging."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True
            )
        return self._client

    async def request(
        self,
        source: str,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        paper_id: Any | None = None,
        job_id: Any | None = None,
    ) -> httpx.Response:
        """Make a rate-limited, circuit-broken HTTP request with retry on 429."""
        host = urlsplit(url).hostname or ""
        if host not in API_HOSTS:
            raise HostNotAllowed(f"{host!r} is not a declared academic-journal API host")
        limiter = get_limiter(source)
        breaker = get_breaker(source)
        client = await self._get_client()

        for attempt in range(_MAX_RETRIES):
            breaker.check(source)
            await limiter.acquire()

            start = time.monotonic()
            error_msg: str | None = None
            status_code: int | None = None
            resp_size: int | None = None

            try:
                resp = await client.request(
                    method, url, params=params, headers=headers
                )
                status_code = resp.status_code
                resp_size = len(resp.content)

                if resp.status_code == 429:
                    default_wait = min(2 * (2 ** attempt), 30)
                    retry_after = _retry_after(resp.headers.get("Retry-After"), default_wait)
                    logger.info(
                        "%s rate limited (429), waiting %.0fs (attempt %d/%d)",
                        source, retry_after, attempt + 1, _MAX_RETRIES,
                    )
                    error_msg = f"429 rate limited, retry_after={retry_after}"
                    await asyncio.sleep(retry_after)
                    continue

                resp.raise_for_status()
                breaker.record_success()
                return resp
            except httpx.HTTPStatusError as exc:
                error_msg = str(exc)
                if exc.response.status_code >= 500:
                    breaker.record_failure()
                raise
            except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
                error_msg = str(exc)
                breaker.record_failure()
                raise
            except Exception as exc:
                error_msg = str(exc)
                raise
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                await _log_call({
                    "source": source,
                    "endpoint": url,
                    "method": method,
                    "request_params": params,
                    "response_status": status_code,
                    "response_size": resp_size,
                    "error": error_msg,
                    "duration_ms": duration_ms,
                    "paper_id": paper_id,
                    "job_id": job_id,
                })

        raise RuntimeError(
            f"{source} rate limited after {_MAX_RETRIES} retries for {url}"
        )

    async def get_json(
        self,
        source: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """Convenience: GET + parse JSON."""
        resp = await self.request(
            source, url, params=params, headers=headers, **kwargs
        )
        return resp.json()

    async def download(
        self,
        url: str,
        dest_path: str,
        *,
        source: str = "pdf_download",
        paper_id: Any | None = None,
        job_id: Any | None = None,
    ) -> tuple[int, str]:
        """Download a file to disk. Returns (size, content_type)."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError(f"refusing to download non-HTTP URL scheme {parts.scheme!r}")
        domain = parts.netloc
        breaker_key = f"pdf:{domain}"
        limiter = get_limiter(source)
        breaker = get_breaker(breaker_key)

        breaker.check(breaker_key)
        await limiter.acquire()

        start = time.monotonic()
        error_msg: str | None = None
        status_code: int | None = None
        resp_size: int | None = None

        try:
            client = await self._get_client()
            resp = await client.get(url)
            status_code = resp.status_code
            resp_size = len(resp.content)
            resp.raise_for_status()
            breaker.record_success()

            Path(dest_path).write_bytes(resp.content)
            content_type = resp.headers.get("content-type", "")
            return resp_size, content_type
        except httpx.HTTPStatusError as exc:
            error_msg = str(exc)
            if exc.response.status_code >= 500:
                breaker.record_failure()
            raise
        except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
            error_msg = str(exc)
            breaker.record_failure()
            raise
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            await _log_call({
                "source": source,
                "endpoint": url,
                "method": "GET",
                "response_status": status_code,
                "response_size": resp_size,
                "error": error_msg,
                "duration_ms": duration_ms,
                "paper_id": paper_id,
                "job_id": job_id,
            })

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
