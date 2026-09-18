"""asyncpg connection pool for the plugin's tables.

The URL comes from :func:`acad.config.database_url` — ``RE_DB_URL`` in the environment,
never a ``.env`` file. A new pool checks the migration ledger before handing out
connections, so plugin code never runs against tables it did not migrate.
"""

from __future__ import annotations

import asyncio

import asyncpg
from research_engine_sdk import PluginConfigError

from acad import config
from acad.db.migrate import require_current

_pool: asyncpg.Pool | None = None
_lock: asyncio.Lock | None = None


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool."""
    global _pool, _lock
    if _pool is not None:
        return _pool
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _pool is None:
            dsn = config.database_url()
            try:
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
            except Exception as exc:
                raise PluginConfigError(
                    f"academic-journal cannot connect to {config.redact_url(dsn)}: "
                    f"{config.redact_text(str(exc)) or type(exc).__name__}"
                ) from None
            try:
                async with pool.acquire() as conn:
                    await require_current(conn)
            except BaseException:
                await pool.close()
                raise
            _pool = pool
    return _pool


async def close_pool() -> None:
    """Close the connection pool."""
    global _pool, _lock
    if _pool is not None:
        await _pool.close()
    _pool = None
    _lock = None
