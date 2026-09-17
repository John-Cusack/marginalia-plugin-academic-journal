"""Base class for acquisition modules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

from acad.models import AcquiredFile, Paper


class AcquisitionModule(ABC):
    """Interface for PDF acquisition strategies."""

    id: ClassVar[str]
    display_name: ClassVar[str]
    priority: ClassVar[int] = 50  # 0-100, higher preferred

    @abstractmethod
    async def can_acquire(self, paper: Paper) -> tuple[float, str]:
        """Return (confidence 0-1, reason) for whether this module can acquire the paper."""

    @abstractmethod
    async def acquire(self, paper: Paper, dest: Path) -> AcquiredFile:
        """Download the paper to dest. Returns AcquiredFile with metadata."""
