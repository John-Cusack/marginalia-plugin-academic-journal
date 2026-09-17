"""asyncpg connection pool, reuses RE_DB_URL."""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg

_pool: asyncpg.Pool | None = None


def _resolve_db_url() -> str:
    """Resolve database URL from environment or .env file."""
    url = os.environ.get("RE_DB_URL")
    if url:
        return url

    # pydantic_settings loads .env into Settings but not os.environ.
    # Fall back to reading .env ourselves.
    for candidate in [Path.cwd() / ".env", Path.home() / ".env"]:
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("RE_DB_URL=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")

    raise RuntimeError(
        "RE_DB_URL not found in environment or .env file. "
        "Set it in your .env or export RE_DB_URL=postgresql+asyncpg://..."
    )


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool."""
    global _pool
    if _pool is None:
        db_url = _resolve_db_url()
        # Core uses SQLAlchemy-style URLs (postgresql+asyncpg://...);
        # asyncpg expects plain postgresql:// URLs.
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
        _pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
