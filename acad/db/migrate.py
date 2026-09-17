"""Idempotent migration runner — executes SQL files on plugin load."""

from __future__ import annotations

from pathlib import Path

from acad.db.pool import get_pool

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def run_migrations() -> None:
    """Run all migration SQL files in order. Idempotent via CREATE IF NOT EXISTS."""
    pool = await get_pool()
    migration_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    async with pool.acquire() as conn:
        for migration_file in migration_files:
            sql = migration_file.read_text()
            await conn.execute(sql)
