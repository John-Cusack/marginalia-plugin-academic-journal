"""Tests for the three-state circuit breaker."""

from __future__ import annotations

import time

import pytest

from acad.infra.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    get_breaker,
    reset_all,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_all()
    yield
    reset_all()


def test_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED


def test_stays_closed_under_threshold():
    cb = CircuitBreaker(failure_threshold=5)
    for _ in range(4):
        cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_opens_at_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_check_raises_when_open():
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure()
    with pytest.raises(CircuitOpenError) as exc_info:
        cb.check("test_source")
    assert exc_info.value.source == "test_source"
    assert exc_info.value.retry_after > 0


def test_success_resets_count():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    # Now we should need 3 more failures to open
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED


def test_transitions_to_half_open():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_success_closes():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
    cb.record_failure()
    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_half_open_failure_reopens():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
    cb.record_failure()
    time.sleep(0.02)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


def test_get_breaker_returns_same_instance():
    b1 = get_breaker("test")
    b2 = get_breaker("test")
    assert b1 is b2


def test_check_passes_when_closed():
    cb = CircuitBreaker()
    cb.check("test")  # Should not raise
