"""Module registry and dispatch for acquisition."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from acad.acquisition_modules.arxiv import ArxivModule
from acad.acquisition_modules.base import AcquisitionModule
from acad.acquisition_modules.direct_pdf import DirectPDFModule
from acad.acquisition_modules.unpaywall import UnpaywallModule

if TYPE_CHECKING:
    from acad.models import Paper

logger = logging.getLogger(__name__)

_modules: list[AcquisitionModule] = []
_initialized = False


def _register_builtins() -> None:
    """Register built-in acquisition modules."""
    global _modules
    _modules = [
        ArxivModule(),
        UnpaywallModule(),
        DirectPDFModule(),
    ]


def _discover_user_modules() -> None:
    """Load user acquisition modules from ``ACAD_MODULES_DIR``, when explicitly set.

    These files execute in the core process with the plugin's approval, so they are
    only read from a directory the operator names; there is no default location.
    """
    configured = os.environ.get("ACAD_MODULES_DIR")
    if not configured:
        return
    user_dir = Path(configured)
    if not user_dir.is_dir():
        logger.warning("ACAD_MODULES_DIR %s is not a directory", user_dir)
        return

    for py_file in user_dir.glob("*.py"):
        try:
            spec = importlib.util.spec_from_file_location(
                f"acad_user_module_{py_file.stem}", py_file
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, AcquisitionModule)
                        and attr is not AcquisitionModule
                    ):
                        instance = attr()
                        _modules.append(instance)
                        logger.info(
                            "Loaded user acquisition module: %s (%s)",
                            instance.id, instance.display_name,
                        )
        except Exception as exc:
            logger.error("Failed to load user module %s: %s", py_file, exc)


def initialize() -> None:
    """Initialize the module registry."""
    global _initialized
    if _initialized:
        return
    _register_builtins()
    _discover_user_modules()
    # Sort by priority (highest first)
    _modules.sort(key=lambda m: m.priority, reverse=True)
    _initialized = True


def get_modules() -> list[AcquisitionModule]:
    """Return all registered modules, sorted by priority."""
    initialize()
    return list(_modules)


async def select_module(paper: Paper) -> tuple[AcquisitionModule, float, str] | None:
    """Select the best acquisition module for a paper.

    Returns (module, confidence, reason) or None if no module can acquire.
    """
    initialize()
    best: tuple[AcquisitionModule, float, str] | None = None

    for module in _modules:
        try:
            confidence, reason = await module.can_acquire(paper)
            if confidence > 0 and (best is None or confidence > best[1]):
                best = (module, confidence, reason)
        except Exception as exc:
            logger.warning(
                "Module %s.can_acquire failed: %s", module.id, exc
            )

    return best
