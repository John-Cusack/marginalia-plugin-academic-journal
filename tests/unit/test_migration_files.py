"""The packaged migration set, without a database."""

from __future__ import annotations

import hashlib

import pytest

from acad.db import migrate


def test_migrations_are_contiguous_and_end_at_current_revision():
    found = migrate.migrations()
    assert [m.revision for m in found] == list(range(1, migrate.CURRENT_REVISION + 1))
    assert [m.name for m in found] == ["literature_pipeline", "citation_graph"]


def test_checksums_are_sha256_of_the_packaged_bytes():
    for migration in migrate.migrations():
        assert migration.checksum == hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()


def test_files_are_read_from_the_installed_package():
    # importlib.resources, not a path relative to the checkout, so this works from a wheel.
    assert "CREATE TABLE IF NOT EXISTS acad_papers" in migrate.migrations()[0].sql


def test_no_downgrade_entry_is_exposed():
    assert not hasattr(migrate, "downgrade")
    assert not any(
        "DROP TABLE" in migration.sql.upper() for migration in migrate.migrations()
    )


def test_ledger_problems_are_reported_not_repaired():
    report = {"problems": ["revision 1 (literature_pipeline) checksum drift: applied a, packaged b"]}
    with pytest.raises(migrate.MigrationError, match="checksum drift"):
        migrate._raise_for_problems(report)
