"""Integration fixtures: a disposable PostgreSQL and the exact core artifact.

These tests never read your ``RE_DB_URL``. The database is either one you name in
``ACAD_TEST_DATABASE_URL`` — whose name must contain ``test``, so the research corpus
cannot be selected by accident — or a throwaway container started here. Core is imported
directly rather than skipped: a missing core artifact is a CI failure, not a reason to
silently pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import research_engine  # noqa: F401  — absent core must fail, never skip
from research_engine_sdk import PluginContext

from acad import config
from acad.db import migrate, pool
from acad.infra import circuit_breaker, rate_limiter

ACAD_TABLES = (
    "acad_pending_citations",
    "acad_api_calls",
    "acad_paper_provenance",
    "acad_discovery_runs",
    "acad_external_identifiers",
    "acad_paper_authors",
    "acad_jobs",
    "acad_papers",
)


def _container_url():
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        try:
            from testcontainers.postgres import PostgresContainer  # testcontainers < 4.13
        except ImportError:  # pragma: no cover - CI installs the integration group
            pytest.fail(
                "No ACAD_TEST_DATABASE_URL and testcontainers is not installed. "
                "Install the integration dependency group or point at a disposable database."
            )
    return PostgresContainer("pgvector/pgvector:pg15", driver=None)


@pytest.fixture(scope="session")
def database_url():
    """A disposable database URL. Never the research corpus."""
    explicit = os.environ.get("ACAD_TEST_DATABASE_URL")
    if explicit:
        name = urlsplit(explicit).path.lstrip("/")
        if "test" not in name.lower():
            pytest.fail(
                f"ACAD_TEST_DATABASE_URL names database {name!r}; integration tests delete "
                "rows, so they only run against a database whose name contains 'test'."
            )
        yield explicit
        return
    container = _container_url()
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


def _sqlalchemy_url(database_url: str) -> str:
    """Core speaks SQLAlchemy URLs; the plugin normalises them back for asyncpg."""
    if database_url.startswith("postgresql+"):
        return database_url
    return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session", autouse=True)
def environment(database_url):
    """Point the plugin at the disposable database for the whole session."""
    with pytest.MonkeyPatch.context() as patch:
        # The same SQLAlchemy-style URL core would export.
        patch.setenv(config.DATABASE_URL_ENV, _sqlalchemy_url(database_url))
        for name in config.SECRET_ENV_VARS:
            patch.delenv(name, raising=False)
        yield


@pytest.fixture(scope="session", autouse=True)
def core_schema(database_url, environment):
    """Apply core's own migrations, so plugin tests see the real core tables."""
    ini = (
        Path(research_engine.__file__).parent
        / "adapters/storage/postgres/migrations/alembic.ini"
    )
    assert ini.is_file(), f"core migrations missing from the installed artifact: {ini}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), "upgrade", "head"],
        env={**os.environ, "RE_DB_URL": _sqlalchemy_url(database_url)},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"core schema migration failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture
async def migrated(database_url):
    """The plugin schema at its current revision, with a clean slate per test."""
    await migrate.upgrade(database_url=database_url)
    await _truncate(database_url)
    config.reset()
    circuit_breaker.reset_all()
    rate_limiter.reset_all()
    yield database_url
    await pool.close_pool()
    await _truncate(database_url)


async def _truncate(database_url: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(config.database_url(database_url))
    try:
        await conn.execute("DELETE FROM core.edges WHERE relation_type = 'cites'")
        for table in ACAD_TABLES:
            if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
                await conn.execute(f"DELETE FROM {table}")
    finally:
        await conn.close()


@pytest.fixture
async def fresh_database(database_url):
    """A database with no plugin tables at all, for migration tests."""
    import asyncpg

    conn = await asyncpg.connect(config.database_url(database_url))
    try:
        await _drop_plugin_tables(conn)
        yield database_url
    finally:
        await pool.close_pool()
        await _drop_plugin_tables(conn)
        await conn.close()


async def _drop_plugin_tables(conn) -> None:
    for table in (*ACAD_TABLES, migrate.LEDGER_TABLE):
        await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture
def context(tmp_path) -> PluginContext:
    return PluginContext(
        plugin_id="academic-journal",
        data_dir=tmp_path / "plugin-data" / "academic-journal",
        distribution_name="marginalia-ai-plugin-academic-journal",
        distribution_version="0.2.0",
    )


@pytest.fixture
async def engine(database_url):
    """A SQLAlchemy engine for direct verification, confined to this helper."""
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(_sqlalchemy_url(database_url), pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()
