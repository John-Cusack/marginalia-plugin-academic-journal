"""Versioned, checksummed migrations for the plugin-owned ``acad_*`` tables.

Core runs these only through ``research-engine plugin migrate academic-journal``, after the
operator has approved this exact distribution; tools never create or alter tables. Core
calls :func:`status` and :func:`upgrade` with ``context`` and ``database_url`` keywords and
reads ``current_revision``/``status`` from the returned mapping.

The ledger (``acad_schema_migrations``) records each applied file's revision and SHA-256.
Upgrades hold a session advisory lock, so concurrent runs serialise instead of racing on
``CREATE TABLE IF NOT EXISTS``, and each file commits atomically with its ledger row.

Two states are refused rather than repaired:

- **drift** — an applied file's checksum no longer matches the packaged file. Re-running
  edited DDL against a database built from the old text proves nothing about its shape.
- **ahead** — the ledger holds a revision this version does not ship, i.e. a newer
  release migrated the database. There is no downgrade path, and nothing is dropped.

Databases created by 0.1.x hold the tables but no ledger. Both 0.1.x files are idempotent
(``IF NOT EXISTS`` throughout), so upgrading them re-runs each file as a no-op and records
it, leaving every row in place.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any

import asyncpg
from research_engine_sdk import PluginConfigError

from acad import __version__, config

if TYPE_CHECKING:
    from research_engine_sdk import PluginContext

CURRENT_REVISION = 2
LEDGER_TABLE = "acad_schema_migrations"

#: First 8 bytes of sha256(b"academic-journal:migrations") as a signed bigint.
ADVISORY_LOCK_KEY = 5232763593119027628

_FILENAME = re.compile(r"^(?P<revision>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")

_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    revision       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    applied_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


class MigrationError(RuntimeError):
    """The database cannot be brought to this version's revision safely."""


@dataclass(frozen=True, slots=True)
class Migration:
    revision: int
    name: str
    sql: str
    checksum: str


def migrations() -> tuple[Migration, ...]:
    """Packaged migration files in revision order, numbered contiguously from 001."""

    found: list[Migration] = []
    for entry in (resources.files("acad.db") / "migrations").iterdir():
        match = _FILENAME.fullmatch(entry.name)
        if match is None:
            continue
        data = entry.read_bytes()
        found.append(
            Migration(
                revision=int(match.group("revision")),
                name=match.group("name"),
                sql=data.decode("utf-8"),
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    found.sort(key=lambda migration: migration.revision)
    revisions = [migration.revision for migration in found]
    if revisions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration files must be numbered 001..N; found {revisions}")
    if revisions[-1:] != [CURRENT_REVISION]:
        raise MigrationError(
            f"CURRENT_REVISION is {CURRENT_REVISION} but the last packaged file is {revisions}"
        )
    return tuple(found)


async def _connect(database_url: str | None) -> asyncpg.Connection:
    dsn = config.database_url(database_url)
    try:
        return await asyncpg.connect(dsn)
    except Exception as exc:
        raise PluginConfigError(
            f"academic-journal cannot connect to {config.redact_url(dsn)}: "
            f"{config.redact_text(str(exc)) or type(exc).__name__}"
        ) from None


async def inspect(conn: Any) -> dict[str, Any]:
    """Compare the ledger with the packaged files. Read-only; never raises on drift."""

    known = {migration.revision: migration for migration in migrations()}
    ledger_exists = await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", LEDGER_TABLE)
    rows = (
        await conn.fetch(
            f"SELECT revision, name, checksum, plugin_version, applied_at "
            f"FROM {LEDGER_TABLE} ORDER BY revision"
        )
        if ledger_exists
        else []
    )

    problems: list[str] = []
    ahead = False
    applied: list[dict[str, Any]] = []
    for row in rows:
        applied.append(dict(row))
        migration = known.get(row["revision"])
        if migration is None:
            ahead = True
            problems.append(
                f"revision {row['revision']} ({row['name']}) was applied by academic-journal "
                f"{row['plugin_version']}, which is newer than {__version__}; "
                "downgrade is not supported"
            )
        elif migration.checksum != row["checksum"]:
            problems.append(
                f"revision {row['revision']} ({row['name']}) checksum drift: applied "
                f"{row['checksum']}, packaged {migration.checksum}"
            )

    applied_revisions = [row["revision"] for row in rows]
    if applied_revisions != list(range(1, len(applied_revisions) + 1)):
        problems.append(f"migration ledger is not contiguous: {applied_revisions}")
    current = max(applied_revisions, default=0)

    if ahead:
        state = "ahead"
    elif problems:
        state = "drift"
    elif current < CURRENT_REVISION:
        state = "pending"
    else:
        state = "ok"

    return {
        "plugin_id": config.PLUGIN_ID,
        "plugin_version": __version__,
        "current_revision": current,
        "target_revision": CURRENT_REVISION,
        "status": state,
        "pending": [
            {"revision": m.revision, "name": m.name}
            for m in known.values()
            if m.revision > current
        ],
        "applied": applied,
        "problems": problems,
    }


def _raise_for_problems(report: dict[str, Any]) -> None:
    if report["problems"]:
        raise MigrationError(
            "academic-journal refuses to migrate: " + "; ".join(report["problems"])
        )


async def status(
    *, context: PluginContext | None = None, database_url: str | None = None
) -> dict[str, Any]:
    """Core's status entry. Raises on drift or a newer database so ``migrate`` fails loudly."""

    del context  # the revision lives in the database, not the data directory
    conn = await _connect(database_url)
    try:
        report = await inspect(conn)
    finally:
        await conn.close()
    _raise_for_problems(report)
    return report


async def upgrade(
    *, context: PluginContext | None = None, database_url: str | None = None
) -> dict[str, Any]:
    """Core's upgrade entry: apply every pending file, each in its own transaction."""

    del context
    conn = await _connect(database_url)
    try:
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await conn.execute(_LEDGER_DDL)
            report = await inspect(conn)
            _raise_for_problems(report)
            applied_now: list[int] = []
            for migration in migrations():
                if migration.revision <= report["current_revision"]:
                    continue
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        f"INSERT INTO {LEDGER_TABLE} "
                        "(revision, name, checksum, plugin_version) VALUES ($1, $2, $3, $4)",
                        migration.revision,
                        migration.name,
                        migration.checksum,
                        __version__,
                    )
                applied_now.append(migration.revision)
            report = await inspect(conn)
            report["applied_now"] = applied_now
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()
    return report


async def require_current(conn: Any) -> None:
    """Refuse to run plugin code against a database at the wrong revision."""

    report = await inspect(conn)
    if report["status"] != "ok":
        detail = "; ".join(report["problems"]) or (
            f"database is at revision {report['current_revision']}, "
            f"academic-journal {__version__} needs {CURRENT_REVISION}"
        )
        raise PluginConfigError(
            f"academic-journal migration required ({detail}). "
            "Run: research-engine plugin migrate academic-journal"
        )
