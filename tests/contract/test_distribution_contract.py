"""Every promise in acad/plugin.yaml, checked against the installed package.

SDK only: no core import, so this suite runs wherever the plugin is installed.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from importlib import metadata
from pathlib import Path

import pytest
import yaml
from research_engine_sdk import (
    FilesystemPermission,
    NetworkPermission,
    SourceSearchProvider,
    entry_module,
    parse_manifest,
)

import acad
from acad.db import migrate
from acad.infra.http_client import API_HOSTS

PACKAGE_ROOT = Path(acad.__file__).parent
DISTRIBUTION = "marginalia-ai-plugin-academic-journal"
ENTRY_POINT_GROUP = "research_engine.plugins"


@pytest.fixture(scope="module")
def manifest():
    return parse_manifest(PACKAGE_ROOT / "plugin.yaml")


def test_manifest_ships_inside_the_import_package():
    assert (PACKAGE_ROOT / "plugin.yaml").is_file()


def test_entry_point_names_the_plugin_id_and_package(manifest):
    entry_points = [
        ep
        for ep in metadata.distribution(DISTRIBUTION).entry_points
        if ep.group == ENTRY_POINT_GROUP
    ]
    assert len(entry_points) == 1
    [entry_point] = entry_points
    assert entry_point.name == manifest.plugin_id == "academic-journal"
    assert entry_point.value == "acad"


def test_distribution_metadata_is_complete():
    meta = metadata.metadata(DISTRIBUTION)
    assert meta["Version"] == acad.__version__
    assert meta["License-Expression"] == "Apache-2.0"
    assert meta["Requires-Python"] == ">=3.11"
    assert (meta["Description"] or meta.get_payload() or "").strip()  # README
    requires = metadata.requires(DISTRIBUTION) or []
    assert any(r.startswith("marginalia-ai-sdk") for r in requires)
    assert not any(r.split()[0] == "marginalia-ai" for r in requires)
    urls = {value.split(",")[0].strip() for value in meta.get_all("Project-URL") or []}
    assert {"Homepage", "Source", "Issues", "Changelog"} <= urls


def test_compatibility_is_declared_for_this_core_series(manifest):
    assert manifest.schema_version == 2
    assert manifest.requires.core_api == ">=0.6.2,<0.7"
    assert manifest.requires.python == ">=3.11"


def test_every_entry_imports_and_exists(manifest):
    for entry in manifest.entry_values():
        module_name, attribute = entry.split(":")
        assert module_name == "acad" or module_name.startswith("acad.")
        assert hasattr(importlib.import_module(module_name), attribute), entry


def test_every_declared_resource_ships(manifest):
    for resource in manifest.resource_paths():
        assert (PACKAGE_ROOT / resource).is_file(), resource


def test_tool_ids_are_namespaced_and_schemas_are_objects(manifest):
    tools = manifest.provides.mcp_tools
    assert len(tools) == 9
    for tool in tools:
        assert tool.id.startswith("academic-journal.")
        assert tool.input_schema["type"] == "object"
        assert isinstance(tool.input_schema.get("properties", {}), dict)
        assert tool.description.strip()


def test_tool_handlers_accept_their_declared_arguments_and_clients(manifest):
    for tool in manifest.provides.mcp_tools:
        module_name, attribute = tool.entry.split(":")
        handler = getattr(importlib.import_module(module_name), attribute)
        assert inspect.iscoroutinefunction(handler), tool.id
        params = inspect.signature(handler).parameters
        for name in tool.input_schema.get("properties", {}):
            assert name in params, f"{tool.id} cannot accept {name!r}"
        for name in tool.input_schema.get("required", []):
            assert params[name].default is inspect.Parameter.empty, f"{tool.id}:{name}"
        # Core passes its scoped clients as keyword arguments.
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), tool.id
        assert "context" in params, tool.id


def test_filter_extension_satisfies_the_search_contract(manifest):
    [contribution] = manifest.provides.filter_extensions
    module_name, attribute = contribution.entry.split(":")
    instance = getattr(importlib.import_module(module_name), attribute)()
    assert instance.filter_id == contribution.id
    assert instance.description.strip()
    assert instance.input_schema["type"] == "object"
    clause = instance.build_clause({"year_min": 2020})
    compiled = str(clause.compile())
    assert "acad_papers" in compiled
    assert "2020" not in compiled  # bound parameter, not interpolated


def test_source_search_provider_satisfies_the_sdk_protocol(manifest):
    [contribution] = manifest.provides.source_search
    module_name, attribute = contribution.entry.split(":")
    provider = getattr(importlib.import_module(module_name), attribute)()
    assert isinstance(provider, SourceSearchProvider)
    assert provider.plugin_name == contribution.id


def test_hook_declares_its_event(manifest):
    [contribution] = manifest.provides.post_ingestion_hooks
    module_name, attribute = contribution.entry.split(":")
    handler = getattr(importlib.import_module(module_name), attribute)
    assert contribution.event == "post_ingestion"
    assert handler._hook_event == "post_ingestion"
    assert handler._hook_document_types


def test_extraction_schema_resource_matches_its_declaration(manifest):
    [contribution] = manifest.provides.extraction_schemas
    definition = yaml.safe_load((PACKAGE_ROOT / contribution.file).read_text())
    assert definition["id"] == contribution.id
    assert definition["version"] == contribution.version
    assert definition["record_types"] and definition["prompt"]


def test_database_declaration_matches_the_packaged_migrations(manifest):
    database = manifest.provides.database
    assert database is not None
    assert database.current_revision == migrate.CURRENT_REVISION
    assert database.current_revision == migrate.migrations()[-1].revision
    for entry in (database.upgrade_entry, database.status_entry):
        assert entry_module(entry) == "acad.db.migrate"


def test_permissions_match_what_the_plugin_does(manifest):
    permissions = manifest.permissions
    # Open-access PDFs come from arbitrary publisher hosts, so egress is not host-limited.
    assert permissions.network is NetworkPermission.full
    assert permissions.filesystem is FilesystemPermission.read_write_plugin_data
    assert permissions.ingest is True  # pipeline calls IngestionClient
    assert permissions.write is True  # citation extraction creates `cites` edges
    assert permissions.llm is False  # extraction runs in core, not here
    assert permissions.subprocess is False  # workers are asyncio tasks


def test_declared_allowlist_covers_every_metadata_api_host(manifest):
    assert set(manifest.permissions.network_allowlist) == set(API_HOSTS)


def test_hard_coded_api_urls_are_inside_the_allowlist():
    """Every URL the package builds targets a declared host.

    Literals handed to string normalisation (``"https://doi.org/"`` stripped off a DOI)
    are not request targets, so they are excluded.
    """
    from urllib.parse import urlsplit

    normalisers = {"replace", "removeprefix", "startswith", "lstrip", "strip"}
    urls: list[str] = []
    for source in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text())
        excluded = {
            id(argument)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in normalisers
            for argument in node.args
        }
        for node in ast.walk(tree):
            # Plain literals, and the leading piece of an f-string such as
            # f"https://arxiv.org/pdf/{arxiv_id}.pdf".
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("https://") and id(node) not in excluded:
                    urls.append(node.value)
            elif isinstance(node, ast.JoinedStr) and node.values:
                head = node.values[0]
                if isinstance(head, ast.Constant) and str(head.value).startswith("https://"):
                    urls.append(str(head.value))

    hosts = {urlsplit(url).hostname for url in urls} - {None}
    assert hosts <= set(API_HOSTS), hosts - set(API_HOSTS)


def test_runtime_package_never_imports_core():
    offenders = []
    for source in PACKAGE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "research_engine" or name.startswith("research_engine."):
                    offenders.append(f"{source.relative_to(PACKAGE_ROOT)}: {name}")
    assert offenders == []


def test_unit_tests_never_import_core():
    offenders = []
    for source in (Path(__file__).parent.parent / "unit").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "research_engine" or name.startswith("research_engine."):
                    offenders.append(f"{source.name}: {name}")
    assert offenders == []
