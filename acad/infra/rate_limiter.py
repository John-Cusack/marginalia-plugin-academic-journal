"""Per-source async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate: float  # tokens per second
    max_tokens: float  # burst capacity
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = self.max_tokens
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.max_tokens, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last_refill = time.monotonic()
            else:
                self._tokens -= 1.0


_registry: dict[str, TokenBucket] = {}

_DEFAULTS: dict[str, tuple[float, float]] = {
    "openalex": (9.0, 9.0),
    "semantic_scholar": (0.2, 1.0),
    "crossref": (45.0, 45.0),
    "unpaywall": (10.0, 10.0),
    "pdf_download": (2.0, 2.0),
    "arxiv": (1.0, 3.0),
    "ncbi": (3.0, 3.0),
    "core": (1.0, 1.0),
}


def get_limiter(source: str) -> TokenBucket:
    if source not in _registry:
        if source == "semantic_scholar" and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
            rate, burst = 10.0, 10.0
        elif source == "ncbi" and os.environ.get("NCBI_API_KEY"):
            rate, burst = 9.0, 9.0
        else:
            rate, burst = _DEFAULTS.get(source, (2.0, 2.0))
        _registry[source] = TokenBucket(rate=rate, max_tokens=burst)
    return _registry[source]


def reset_all() -> None:
    """Reset all limiters (for testing)."""
    _registry.clear()
