"""Discovery, approval, migration, and loading against core 0.6.

Everything here goes through core's own discovery/activation/loader objects, which is
what ``research-engine plugin …`` drives.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx
import sqlalchemy as sa
from research_engine.domain.provenance import PluginActivationState
from research_engine.plugins.activation import PluginActivationManager, PluginMigrationError
from research_engine.plugins.discovery import scan_plugins
from research_engine.plugins.loader import PluginLoader
from research_engine.plugins.registry import PluginRegistry
from research_engine_sdk import SourceMatch, SourceQuery

from acad.db import migrate
from acad.db import queries as db

pytestmark = pytest.mark.integration

PLUGIN_ID = "academic-journal"
TOOL_IDS = {
    "academic-journal.discover_papers",
    "academic-journal.discover_by_doi",
    "academic-journal.discover_by_author",
    "academic-journal.pipeline_status",
    "academic-journal.search_papers",
    "academic-journal.retry_failed",
    "academic-journal.import_manual_pdf",
    "academic-journal.start_workers",
    "academic-journal.stop_workers",
}


class MemoryActivations:
    """The activation repository core persists to, kept in memory for the test."""

    def __init__(self) -> None:
        self.row = None

    async def save(self, activation):
        self.row = activation

    async def get(self, plugin_id):
        return self.row if self.row and self.row.plugin_id == plugin_id else None

    async def list_all(self):
        return [self.row] if self.row else []

    async def list_enabled(self):
        return [self.row] if self.row and self.row.enabled else []

    async def update_state(self, plugin_id, state, *, enabled=None, last_error=None, last_seen_at=None):
        self.row = self.row.model_copy(update={
            "state": state,
            "enabled": self.row.enabled if enabled is None else enabled,
            "last_error": last_error,
            "last_seen_at": last_seen_at or self.row.last_seen_at,
        })

    async def record_migration(self, plugin_id, *, revision, status, state, last_error=None):
        self.row = self.row.model_copy(update={
            "database_revision": revision,
            "database_status": status,
            "state": state,
            "last_error": last_error,
        })

    async def delete(self, plugin_id):
        self.row = None


@pytest.fixture
def discovered():
    matches = [p for p in scan_plugins().plugins if p.plugin_id == PLUGIN_ID]
    assert len(matches) == 1, "the plugin distribution is not installed in this environment"
    return matches[0]


@pytest.fixture
def activations():
    return MemoryActivations()


@pytest.fixture
def manager(activations):
    return PluginActivationManager(activations)


def test_installed_distribution_is_discovered_without_importing_the_package():
    """Reading the manifest must not execute plugin code."""
    script = (
        "import sys\n"
        "from research_engine.plugins.discovery import scan_plugins\n"
        "plugins = {p.plugin_id: p for p in scan_plugins().plugins}\n"
        "assert 'academic-journal' in plugins, plugins\n"
        "assert 'acad' not in sys.modules, 'discovery imported the plugin package'\n"
        "print(plugins['academic-journal'].distribution_name)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "research-engine-plugin-academic-journal"


async def test_audit_reports_every_contribution_and_the_database_revision(manager, discovered):
    status = await manager.audit(PLUGIN_ID, discovered_plugins=[discovered])

    assert status.state is PluginActivationState.available
    manifest = status.discovered.manifest
    assert {tool.id for tool in manifest.provides.mcp_tools} == TOOL_IDS
    assert [f.id for f in manifest.provides.filter_extensions] == ["academic_paper"]
    assert [s.id for s in manifest.provides.source_search] == ["acad"]
    assert [h.id for h in manifest.provides.post_ingestion_hooks] == ["academic-journal.citation_hook"]
    assert [s.id for s in manifest.provides.extraction_schemas] == ["bibliography_references"]
    assert manifest.provides.database.current_revision == migrate.CURRENT_REVISION
    permissions = manifest.permissions.model_dump(mode="json")
    assert permissions["ingest"] and permissions["write"] and not permissions["llm"]
    assert status.discovered.manifest_sha256


async def test_enable_records_the_exact_version_and_hash(manager, activations, discovered):
    activation = await manager.approve(
        PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered]
    )

    assert activation.distribution_name == "research-engine-plugin-academic-journal"
    assert activation.distribution_version == discovered.distribution_version
    assert activation.manifest_sha256 == discovered.manifest_sha256
    assert activation.entry_point_name == PLUGIN_ID
    assert activation.enabled and activation.approved_at is not None
    assert activation.permissions_granted["ingest"] is True


async def test_load_is_refused_until_migrate_then_registers_everything(
    manager, activations, discovered, fresh_database, tmp_path
):
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(activations, registry, tmp_path / "plugin-data")

    # Declared revision 2, recorded revision none: core must not load the plugin.
    status = await manager.audit(PLUGIN_ID, discovered_plugins=[discovered])
    assert status.state is PluginActivationState.error
    assert "migration required" in status.reason
    assert await loader.load_enabled([discovered]) == []
    assert registry.get_mcp_tools() == {}

    result = await manager.migrate(
        PLUGIN_ID,
        database_url=fresh_database,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    assert result["current_revision"] == 2
    assert result["previous_revision"] == 0
    assert activations.row.database_revision == 2

    assert await loader.load_enabled([discovered]) == [PLUGIN_ID]
    assert set(registry.get_mcp_tools()) == TOOL_IDS
    assert set(registry.get_filter_extensions()) == {"academic_paper"}
    assert set(registry.get_source_search_providers()) == {"acad"}
    assert registry.get_post_ingestion_hooks("academic_journal")
    assert [(sid, ver) for sid, ver, _def, _owner in registry.get_extraction_schemas()] == [
        ("bibliography_references", 1)
    ]


async def test_failed_contribution_leaves_nothing_registered(
    manager, activations, discovered, migrated, tmp_path
):
    """A staged load commits completely or not at all."""
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        PLUGIN_ID,
        database_url=migrated,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    broken_manifest = discovered.manifest.model_copy(deep=True)
    broken_tool = broken_manifest.provides.mcp_tools[0].model_copy(
        update={"entry": "acad.tools.discover_papers:does_not_exist"}
    )
    object.__setattr__(
        broken_manifest.provides, "mcp_tools",
        [broken_tool, *broken_manifest.provides.mcp_tools[1:]],
    )
    broken = dataclasses.replace(discovered, manifest=broken_manifest)

    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(activations, registry, tmp_path / "plugin-data")
    assert await loader.load_enabled([broken]) == []

    assert registry.get_mcp_tools() == {}
    assert registry.get_filter_extensions() == {}
    assert registry.get_source_search_providers() == {}


async def test_changed_version_or_hash_needs_reapproval(
    manager, activations, discovered, migrated, tmp_path
):
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        PLUGIN_ID,
        database_url=migrated,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    upgraded = dataclasses.replace(discovered, distribution_version="0.3.0")

    status = await manager.audit(PLUGIN_ID, discovered_plugins=[upgraded])
    assert status.state is PluginActivationState.pending_approval

    loader = PluginLoader(activations, PluginRegistry(), tmp_path / "plugin-data")
    assert await loader.load_enabled([upgraded]) == []

    retouched = dataclasses.replace(discovered, manifest_sha256="0" * 64)
    status = await manager.audit(PLUGIN_ID, discovered_plugins=[retouched])
    assert status.state is PluginActivationState.pending_approval


async def test_migrate_refuses_without_an_exact_approval(manager, discovered, fresh_database, tmp_path):
    with pytest.raises(PluginMigrationError, match="exact approval"):
        await manager.migrate(
            PLUGIN_ID,
            database_url=fresh_database,
            data_root=tmp_path / "plugin-data",
            discovered_plugins=[discovered],
        )


async def test_uninstalled_distribution_keeps_literature_tables_and_edges(
    manager, activations, discovered, migrated, engine, tmp_path
):
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        PLUGIN_ID,
        database_url=migrated,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    paper = await db.insert_paper({"title": "Survives uninstall"})
    document_id = uuid4()
    await db.update_paper_document_id(paper, document_id)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO core.edges (id, source_kind, source_id, target_kind, target_id, "
                "relation_type, attributes, confidence) VALUES (:id, 'document', :src, "
                "'document', :tgt, 'cites', '{}', 1.0)"
            ),
            {"id": uuid4(), "src": document_id, "tgt": uuid4()},
        )

    # The distribution is gone; nothing is discovered any more.
    [status] = await manager.inventory(discovered_plugins=[], issues=[])
    assert status.state is PluginActivationState.missing
    assert status.activation.manifest_sha256 == discovered.manifest_sha256

    assert (await db.get_paper(paper))["title"] == "Survives uninstall"
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                sa.text("SELECT COUNT(*) FROM core.edges WHERE relation_type='cites'")
            )
        ).scalar() == 1


@respx.mock
async def test_registered_source_provider_returns_sdk_dtos_and_degrades(
    manager, activations, discovered, migrated, tmp_path, openalex_works_response
):
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        PLUGIN_ID,
        database_url=migrated,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(activations, registry, tmp_path / "plugin-data")
    await loader.load_enabled([discovered])
    provider = registry.get_source_search_providers()["acad"]

    route = respx.get("https://api.openalex.org/works")
    route.mock(return_value=httpx.Response(200, json=openalex_works_response))
    matches = await provider.search(SourceQuery(query="attention"), limit=5)
    assert matches and all(isinstance(m, SourceMatch) for m in matches)
    assert matches[0].ingest_action.tool in TOOL_IDS

    # A provider outage returns nothing instead of breaking core's fan-out.
    route.mock(return_value=httpx.Response(500))
    assert await provider.search(SourceQuery(query="attention"), limit=5) == []


async def test_loaded_plugin_receives_scoped_clients_and_its_data_dir(
    manager, activations, discovered, migrated, tmp_path
):
    await manager.approve(PLUGIN_ID, non_interactive=True, discovered_plugins=[discovered])
    await manager.migrate(
        PLUGIN_ID,
        database_url=migrated,
        data_root=tmp_path / "plugin-data",
        discovered_plugins=[discovered],
    )
    registry = PluginRegistry()
    registry.register_core_types()
    loader = PluginLoader(
        activations,
        registry,
        tmp_path / "plugin-data",
        ingestion=AsyncMock(),
        edge=AsyncMock(),
        extraction=AsyncMock(),
    )
    await loader.load_enabled([discovered])

    clients = loader.build_plugin_clients(PLUGIN_ID)

    context = clients["context"]
    assert context.plugin_id == PLUGIN_ID
    assert context.data_dir == (tmp_path / "plugin-data" / PLUGIN_ID).resolve()
    assert context.distribution_name == "research-engine-plugin-academic-journal"
    # ingest and write are granted; llm is not.
    assert not type(clients["ingestion"]).__name__.startswith("Denied")
    assert not type(clients["edge"]).__name__.startswith("Denied")
    assert type(clients["llm"]).__name__ == "DeniedLLMClient"
