"""The plugin migration lifecycle against a real PostgreSQL."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from research_engine_sdk import PluginConfigError

from acad import config
from acad.db import migrate, pool

pytestmark = pytest.mark.integration


async def _connect(database_url: str) -> asyncpg.Connection:
    return await asyncpg.connect(config.database_url(database_url))


async def test_empty_database_reaches_revision_2(fresh_database):
    report = await migrate.upgrade(database_url=fresh_database)

    assert report["current_revision"] == 2
    assert report["status"] == "ok"
    assert report["applied_now"] == [1, 2]
    conn = await _connect(fresh_database)
    try:
        for table in ("acad_papers", "acad_jobs", "acad_pending_citations"):
            assert await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
        rows = await conn.fetch(
            f"SELECT revision, name, checksum FROM {migrate.LEDGER_TABLE} ORDER BY revision"
        )
        assert [row["revision"] for row in rows] == [1, 2]
        assert [row["checksum"] for row in rows] == [m.checksum for m in migrate.migrations()]
    finally:
        await conn.close()


async def test_repeat_upgrade_is_a_no_op(fresh_database):
    await migrate.upgrade(database_url=fresh_database)
    conn = await _connect(fresh_database)
    try:
        before = await conn.fetch(
            f"SELECT revision, applied_at FROM {migrate.LEDGER_TABLE} ORDER BY revision"
        )
        report = await migrate.upgrade(database_url=fresh_database)
        after = await conn.fetch(
            f"SELECT revision, applied_at FROM {migrate.LEDGER_TABLE} ORDER BY revision"
        )
    finally:
        await conn.close()

    assert report["applied_now"] == []
    assert report["current_revision"] == 2
    assert [dict(r) for r in before] == [dict(r) for r in after]


async def test_existing_0_1_x_database_upgrades_without_losing_data(fresh_database):
    """0.1.x created the tables with no ledger. Upgrading adopts them in place."""
    conn = await _connect(fresh_database)
    try:
        for migration in migrate.migrations():
            await conn.execute(migration.sql)  # what 0.1.x's runner did
        paper_id = await conn.fetchval(
            "INSERT INTO acad_papers (title, year, pipeline_stage, stage_status) "
            "VALUES ('Existing paper', 2019, 'complete', 'succeeded') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO acad_external_identifiers (paper_id, source, external_id) "
            "VALUES ($1, 'doi', '10.1/existing')",
            paper_id,
        )
        assert not await conn.fetchval(
            "SELECT to_regclass($1) IS NOT NULL", migrate.LEDGER_TABLE
        )

        report = await migrate.upgrade(database_url=fresh_database)

        assert report["current_revision"] == 2
        assert report["applied_now"] == [1, 2]
        assert await conn.fetchval("SELECT COUNT(*) FROM acad_papers") == 1
        row = await conn.fetchrow("SELECT title, year, pipeline_stage FROM acad_papers")
        assert dict(row) == {
            "title": "Existing paper", "year": 2019, "pipeline_stage": "complete"
        }
        assert await conn.fetchval(
            "SELECT external_id FROM acad_external_identifiers WHERE paper_id = $1", paper_id
        ) == "10.1/existing"
    finally:
        await conn.close()


async def test_checksum_drift_is_refused(fresh_database):
    await migrate.upgrade(database_url=fresh_database)
    conn = await _connect(fresh_database)
    try:
        await conn.execute(
            f"UPDATE {migrate.LEDGER_TABLE} SET checksum = 'tampered' WHERE revision = 1"
        )

        with pytest.raises(migrate.MigrationError, match="checksum drift"):
            await migrate.upgrade(database_url=fresh_database)
        with pytest.raises(migrate.MigrationError, match="checksum drift"):
            await migrate.status(database_url=fresh_database)

        report = await migrate.inspect(conn)
        assert report["status"] == "drift"
        # Refusal changed nothing.
        assert await conn.fetchval(
            f"SELECT COUNT(*) FROM {migrate.LEDGER_TABLE}"
        ) == 2
    finally:
        await conn.close()


async def test_database_from_a_newer_release_is_refused_and_untouched(fresh_database):
    await migrate.upgrade(database_url=fresh_database)
    conn = await _connect(fresh_database)
    try:
        await conn.execute(
            f"INSERT INTO {migrate.LEDGER_TABLE} (revision, name, checksum, plugin_version) "
            "VALUES (3, 'future_feature', 'abc', '0.3.0')"
        )

        with pytest.raises(migrate.MigrationError, match="downgrade is not supported"):
            await migrate.status(database_url=fresh_database)
        with pytest.raises(migrate.MigrationError, match="downgrade is not supported"):
            await migrate.upgrade(database_url=fresh_database)

        assert await conn.fetchval(
            f"SELECT COUNT(*) FROM {migrate.LEDGER_TABLE} WHERE revision = 3"
        ) == 1
        assert await conn.fetchval("SELECT to_regclass('acad_papers') IS NOT NULL")
    finally:
        await conn.close()


async def test_concurrent_upgrades_serialise(fresh_database):
    results = await asyncio.gather(
        *(migrate.upgrade(database_url=fresh_database) for _ in range(4))
    )

    assert all(report["current_revision"] == 2 for report in results)
    # Exactly one run applied each revision; the others waited on the advisory lock
    # and found the work done.
    assert sorted(len(report["applied_now"]) for report in results) == [0, 0, 0, 2]
    conn = await _connect(fresh_database)
    try:
        assert await conn.fetchval(f"SELECT COUNT(*) FROM {migrate.LEDGER_TABLE}") == 2
    finally:
        await conn.close()


async def test_status_reports_pending_before_any_migration(fresh_database):
    report = await migrate.status(database_url=fresh_database)

    assert report["current_revision"] == 0
    assert report["status"] == "pending"
    assert [item["revision"] for item in report["pending"]] == [1, 2]


async def test_plugin_refuses_to_run_against_an_unmigrated_database(fresh_database):
    with pytest.raises(PluginConfigError, match="migration required"):
        await pool.get_pool()


async def test_pool_works_once_migrated(migrated):
    connection_pool = await pool.get_pool()
    async with connection_pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM acad_papers") == 0
