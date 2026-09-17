"""Three-state circuit breaker for external APIs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, source: str, retry_after: float) -> None:
        self.source = source
        self.retry_after = retry_after
        super().__init__(f"Circuit open for {source}, retry after {retry_after:.0f}s")


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    _state: CircuitState = field(init=False, default=CircuitState.CLOSED)
    _failure_count: int = field(init=False, default=0)
    _last_failure_time: float = field(init=False, default=0.0)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def check(self, source: str = "") -> None:
        current = self.state
        if current == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            raise CircuitOpenError(source, self.recovery_timeout - elapsed)

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN


_registry: dict[str, CircuitBreaker] = {}


def get_breaker(source: str) -> CircuitBreaker:
    if source not in _registry:
        _registry[source] = CircuitBreaker()
    return _registry[source]


def reset_all() -> None:
    """Reset all breakers (for testing)."""
    _registry.clear()
