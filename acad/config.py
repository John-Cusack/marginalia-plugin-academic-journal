"""Explicit runtime configuration: database URL, plugin data directory, secret redaction.

Nothing here reads a ``.env`` file. Core loads its own ``.env`` into its settings object,
not into the process environment, so a plugin that went looking for one would read a
different file depending on the working directory it was started from — or a stale one
in ``$HOME``. Configuration therefore arrives one of three ways, in this order:

1. a ``database_url`` passed explicitly (core's ``plugin migrate`` capability, or tests);
2. ``RE_DB_URL`` exported into the environment of the process running core;
3. nothing — which is a :class:`PluginConfigError`, never a guessed default.

Mutable state lives under ``PluginContext.data_dir``, which core supplies to every tool
call and to the migration entries.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from research_engine_sdk import PluginConfigError

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext

PLUGIN_ID = "academic-journal"
DATABASE_URL_ENV = "RE_DB_URL"
JOB_LEASE_ENV = "ACAD_JOB_LEASE_SECONDS"
DEFAULT_JOB_LEASE_SECONDS = 3600.0

#: Environment variables whose values are credentials or personal contact details.
SECRET_ENV_VARS = (
    "SEMANTIC_SCHOLAR_API_KEY",
    "NCBI_API_KEY",
    "CORE_API_KEY",
    "OPENALEX_EMAIL",
    "UNPAYWALL_EMAIL",
)
REDACTED = "[redacted]"

_data_dir: Path | None = None

_SENSITIVE_NAME = re.compile(
    r"(?:.*[_-])?(?:api[_-]?key|apikey|key|token|password|secret|email|mailto)",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?P<prefix>[?&](?:api[_-]?key|apikey|key|token|password|secret|email|mailto)=)"
    r"[^&\s'\"#]+",
    re.IGNORECASE,
)
_URL_PASSWORD = re.compile(r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(?=@)", re.I)
_BEARER = re.compile(r"(?P<prefix>\bBearer\s+)\S+", re.IGNORECASE)


def bind_context(context: PluginContext | None) -> None:
    """Adopt the data directory core resolved for this plugin."""

    global _data_dir
    if context is None:
        return
    if context.plugin_id != PLUGIN_ID:
        raise PluginConfigError(
            f"academic-journal received a context for plugin {context.plugin_id!r}"
        )
    _data_dir = Path(context.data_dir)


def reset() -> None:
    """Forget the bound context (tests)."""

    global _data_dir
    _data_dir = None


def database_url(explicit: str | None = None) -> str:
    """The asyncpg DSN for the plugin's tables. Explicit beats ``RE_DB_URL``."""

    url = explicit or os.environ.get(DATABASE_URL_ENV)
    if not url:
        raise PluginConfigError(
            "academic-journal has no database URL. Export RE_DB_URL in the environment "
            "of the process running research-engine (for MCP, the server's env block). "
            "The plugin does not read .env files."
        )
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def data_dir() -> Path:
    if _data_dir is None:
        raise PluginConfigError(
            "academic-journal has no plugin data directory. Core supplies it as "
            "PluginContext.data_dir when it runs a tool; call a tool before starting work."
        )
    return _data_dir


def papers_dir() -> Path:
    """Where acquired PDFs are written. Created on first use."""

    path = data_dir() / "papers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_lease_seconds() -> float:
    """How long an ``in_progress`` job may stay locked before another worker reclaims it."""

    raw = os.environ.get(JOB_LEASE_ENV)
    if not raw:
        return DEFAULT_JOB_LEASE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise PluginConfigError(f"{JOB_LEASE_ENV} must be a number of seconds") from exc
    if value <= 0:
        raise PluginConfigError(f"{JOB_LEASE_ENV} must be positive")
    return value


def redact_url(url: str) -> str:
    """A DSN with its password removed, for messages."""

    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.rsplit("@", 1)
    user = parts.username or ""
    return urlunsplit(parts._replace(netloc=f"{user}:{REDACTED}@{netloc[-1]}"))


def redact_text(text: str) -> str:
    """Scrub credentials and contact details from free text (error messages, logs)."""

    text = _QUERY_SECRET.sub(lambda m: m.group("prefix") + REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: m.group("prefix") + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group("prefix") + REDACTED, text)
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            text = text.replace(value, REDACTED)
    return text


def redact_params(params: dict | None) -> dict | None:
    """Request parameters safe to persist: sensitive names keep their key, lose their value."""

    if params is None:
        return None
    return {
        key: REDACTED if _SENSITIVE_NAME.fullmatch(str(key)) else value
        for key, value in params.items()
    }
