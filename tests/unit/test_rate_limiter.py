"""Tests for the token bucket rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from acad.infra.rate_limiter import TokenBucket, get_limiter, reset_all


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_all()
    yield
    reset_all()


@pytest.mark.asyncio
async def test_bucket_allows_burst():
    """A bucket with max_tokens=3 should allow 3 immediate acquires."""
    bucket = TokenBucket(rate=1.0, max_tokens=3.0)
    start = time.monotonic()
    for _ in range(3):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # All 3 should complete nearly instantly (burst)
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_bucket_throttles_after_burst():
    """After burst is exhausted, acquire should sleep."""
    bucket = TokenBucket(rate=10.0, max_tokens=1.0)
    # First acquire is instant (uses the 1 token)
    await bucket.acquire()
    # Second should wait ~0.1s (1/rate)
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05  # At least some sleep happened


@pytest.mark.asyncio
async def test_bucket_refills_over_time():
    """Tokens should refill based on elapsed time."""
    bucket = TokenBucket(rate=100.0, max_tokens=5.0)
    # Drain all tokens
    for _ in range(5):
        await bucket.acquire()
    # Wait for refill
    await asyncio.sleep(0.06)  # Should refill ~6 tokens at 100/s
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05  # Should be instant since tokens refilled


def test_get_limiter_returns_same_instance():
    limiter1 = get_limiter("openalex")
    limiter2 = get_limiter("openalex")
    assert limiter1 is limiter2


def test_get_limiter_uses_defaults():
    limiter = get_limiter("openalex")
    assert limiter.rate == 9.0
    assert limiter.max_tokens == 9.0


def test_get_limiter_unknown_source_gets_default():
    limiter = get_limiter("unknown_source")
    assert limiter.rate == 2.0
    assert limiter.max_tokens == 2.0
