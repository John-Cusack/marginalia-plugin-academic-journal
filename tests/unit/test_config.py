"""Configuration comes from explicit arguments and the environment — never a .env file."""

from __future__ import annotations

import pytest
from research_engine_sdk import PluginConfigError, PluginContext

from acad import config
from acad.db import pool


def _context(tmp_path, plugin_id: str = "academic-journal") -> PluginContext:
    return PluginContext(
        plugin_id=plugin_id,
        data_dir=tmp_path / "plugin-data" / plugin_id,
        distribution_name="research-engine-plugin-academic-journal",
        distribution_version="0.2.0",
    )


def test_explicit_url_beats_environment(monkeypatch):
    monkeypatch.setenv("RE_DB_URL", "postgresql+asyncpg://env:pw@envhost/env_db")
    assert config.database_url("postgresql://explicit@host/db") == "postgresql://explicit@host/db"


def test_environment_url_is_normalised_for_asyncpg(monkeypatch):
    monkeypatch.setenv("RE_DB_URL", "postgresql+asyncpg://u:pw@h:5432/d")
    assert config.database_url() == "postgresql://u:pw@h:5432/d"


def test_missing_url_is_a_config_error_naming_the_variable():
    with pytest.raises(PluginConfigError, match="RE_DB_URL"):
        config.database_url()


async def test_pool_refuses_without_configuration():
    with pytest.raises(PluginConfigError, match="RE_DB_URL"):
        await pool.get_pool()


def test_dotenv_files_are_ignored_in_cwd_and_home(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    checkout.mkdir()
    home.mkdir()
    for directory in (checkout, home):
        (directory / ".env").write_text("RE_DB_URL=postgresql://leaked:pw@corpus/research_engine\n")
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(PluginConfigError):
        config.database_url()


def test_data_dir_requires_core_context():
    with pytest.raises(PluginConfigError, match="data directory"):
        config.data_dir()


def test_papers_dir_lives_under_context_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a checkout-relative path would land here
    context = _context(tmp_path)
    config.bind_context(context)

    papers = config.papers_dir()

    assert papers == context.data_dir / "papers"
    assert papers.is_dir()
    assert not (tmp_path / "papers").exists()


def test_context_for_another_plugin_is_rejected(tmp_path):
    with pytest.raises(PluginConfigError):
        config.bind_context(_context(tmp_path, plugin_id="history"))


def test_none_context_keeps_existing_binding(tmp_path):
    config.bind_context(_context(tmp_path))
    config.bind_context(None)
    assert config.data_dir() == tmp_path / "plugin-data" / "academic-journal"


def test_redact_url_hides_password_only():
    redacted = config.redact_url("postgresql://re_dev:s3cret@db.local:5432/corpus")
    assert "s3cret" not in redacted
    assert redacted == "postgresql://re_dev:[redacted]@db.local:5432/corpus"


def test_redact_text_scrubs_query_secrets_bearer_dsn_and_env_values(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "core-key-12345")
    text = (
        "Client error '403' for url 'https://eutils.ncbi.nlm.nih.gov/elink.fcgi?id=1&api_key=NCBISECRET' "
        "and https://api.unpaywall.org/v2/10.1/x?email=me@example.org; "
        "Authorization: Bearer abc.def; dsn postgresql://u:hunter2@h/d; raw core-key-12345"
    )
    redacted = config.redact_text(text)
    for secret in ("NCBISECRET", "me@example.org", "abc.def", "hunter2", "core-key-12345"):
        assert secret not in redacted
    assert "id=1" in redacted


def test_redact_params_keeps_keys_and_harmless_values():
    params = {"api_key": "k", "email": "e@x", "mailto": "m@x", "search": "attention", "per_page": "5"}
    assert config.redact_params(params) == {
        "api_key": config.REDACTED,
        "email": config.REDACTED,
        "mailto": config.REDACTED,
        "search": "attention",
        "per_page": "5",
    }


def test_job_lease_default_and_validation(monkeypatch):
    assert config.job_lease_seconds() == config.DEFAULT_JOB_LEASE_SECONDS
    monkeypatch.setenv(config.JOB_LEASE_ENV, "120")
    assert config.job_lease_seconds() == 120.0
    monkeypatch.setenv(config.JOB_LEASE_ENV, "0")
    with pytest.raises(PluginConfigError):
        config.job_lease_seconds()
