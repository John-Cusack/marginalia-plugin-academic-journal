"""What the built wheel and sdist contain — and what they must not.

Point ACAD_DIST_DIR at a directory holding freshly built artifacts:

    uv build --out-dir dist && ACAD_DIST_DIR=dist pytest tests/artifact -q
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.artifact

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.2.0"
FORBIDDEN = (".env", ".pyc", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".db", ".sqlite")


@pytest.fixture(scope="module")
def dist_dir() -> Path:
    configured = os.environ.get("ACAD_DIST_DIR")
    if not configured:
        pytest.fail("ACAD_DIST_DIR is not set; build the distributions first (uv build).")
    path = Path(configured)
    assert path.is_dir(), f"{path} is not a directory"
    return path


@pytest.fixture(scope="module")
def wheel(dist_dir) -> zipfile.ZipFile:
    wheels = sorted(dist_dir.glob("research_engine_plugin_academic_journal-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"
    with zipfile.ZipFile(wheels[0]) as archive:
        yield archive


@pytest.fixture(scope="module")
def sdist(dist_dir) -> tarfile.TarFile:
    sdists = sorted(dist_dir.glob("research_engine_plugin_academic_journal-*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, found {sdists}"
    with tarfile.open(sdists[0]) as archive:
        yield archive


def test_wheel_declares_the_plugin_entry_point(wheel):
    entry_points = wheel.read(
        f"research_engine_plugin_academic_journal-{VERSION}.dist-info/entry_points.txt"
    ).decode()
    assert "[research_engine.plugins]" in entry_points
    assert "academic-journal = acad" in entry_points


def test_wheel_metadata_is_complete(wheel):
    raw = wheel.read(
        f"research_engine_plugin_academic_journal-{VERSION}.dist-info/METADATA"
    )
    meta = BytesParser().parsebytes(raw)
    assert meta["Name"] == "research-engine-plugin-academic-journal"
    assert meta["Version"] == VERSION
    assert meta["License-Expression"] == "Apache-2.0"
    assert meta["Requires-Python"] == ">=3.11"
    long_description = meta["Description"] or meta.get_payload() or ""
    assert "Research Engine Academic Journal Plugin" in long_description
    assert meta["Description-Content-Type"] == "text/markdown"
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    requires = [Requirement(value) for value in meta.get_all("Requires-Dist") or []]
    runtime = {r.name: r for r in requires if not r.marker}
    assert runtime["research-engine-sdk"].specifier == SpecifierSet(">=0.6,<0.7")
    # Core is never a runtime dependency of a plugin distribution.
    assert "research-engine" not in {r.name for r in requires}
    urls = {value.split(",")[0].strip() for value in meta.get_all("Project-URL") or []}
    assert {"Homepage", "Source", "Issues", "Changelog"} <= urls


def test_wheel_ships_the_license_file(wheel):
    names = wheel.namelist()
    assert any(name.endswith("licenses/LICENSE") or name.endswith("/LICENSE") for name in names)


def test_wheel_ships_the_manifest_schema_and_migrations(wheel):
    names = set(wheel.namelist())
    assert "acad/plugin.yaml" in names
    assert "acad/schemas/extraction_schemas/bibliography_references.yaml" in names
    assert "acad/db/migrations/001_literature_pipeline.sql" in names
    assert "acad/db/migrations/002_citation_graph.sql" in names


def test_every_manifest_resource_and_entry_module_is_in_the_wheel(wheel):
    import yaml
    from research_engine_sdk import entry_module, parse_manifest_bytes

    manifest = parse_manifest_bytes(wheel.read("acad/plugin.yaml"))
    names = set(wheel.namelist())
    for resource in manifest.resource_paths():
        assert f"acad/{resource}" in names, resource
    for entry in manifest.entry_values():
        module = entry_module(entry).replace(".", "/")
        assert f"{module}.py" in names or f"{module}/__init__.py" in names, entry
    assert yaml.safe_load(wheel.read("acad/plugin.yaml"))["plugin_id"] == "academic-journal"


def test_wheel_carries_no_tests_caches_or_secrets(wheel):
    names = wheel.namelist()
    assert not any(name.startswith("tests/") for name in names)
    for name in names:
        assert not any(bad in name for bad in FORBIDDEN), name


def test_wheel_contains_only_committed_package_files(wheel):
    """The artifact must be reproducible from the public repository."""
    tracked = subprocess.run(
        ["git", "ls-files", "acad"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    packaged = {
        name for name in wheel.namelist()
        if name.startswith("acad/") and not name.endswith("/")
    }
    assert packaged == set(tracked), packaged.symmetric_difference(tracked)


def test_sdist_holds_the_sources_needed_to_rebuild_and_test(sdist):
    names = set(sdist.getnames())
    root = f"research_engine_plugin_academic_journal-{VERSION}"
    for member in (
        "pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md",
        "acad/plugin.yaml", "acad/db/migrations/001_literature_pipeline.sql",
        "acad/schemas/extraction_schemas/bibliography_references.yaml",
        "tests/unit/test_config.py", "tests/fixtures/sample.pdf",
    ):
        assert f"{root}/{member}" in names, member


def test_sdist_carries_no_caches_or_secrets(sdist):
    for name in sdist.getnames():
        assert not any(bad in name for bad in FORBIDDEN), name


def test_no_downloaded_pdfs_or_worker_state_are_packaged(wheel, sdist):
    packaged = [*wheel.namelist(), *sdist.getnames()]
    pdfs = [name for name in packaged if name.endswith(".pdf")]
    # Only the synthetic zero-page fixture, and only in the sdist.
    assert all(name.endswith("tests/fixtures/sample.pdf") for name in pdfs), pdfs
    assert not any("papers/" in name for name in packaged)
