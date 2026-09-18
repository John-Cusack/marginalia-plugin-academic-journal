# Releasing

The distribution is **`marginalia-ai-plugin-academic-journal`**. It is published by
`.github/workflows/release.yml` through PyPI Trusted Publishing — there is no API token
secret, and `id-token: write` is granted only to the publish jobs.

Nothing has been published yet, under this name or any earlier one.

## Trusted Publisher configuration

Both entries are *pending* publishers: create them before the first upload, while the
project name is still unregistered.

### PyPI — https://pypi.org/manage/account/publishing/

| Field | Value |
|---|---|
| PyPI Project Name | `marginalia-ai-plugin-academic-journal` |
| Owner | `John-Cusack` |
| Repository name | `marginalia-plugin-academic-journal` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

### TestPyPI — https://test.pypi.org/manage/account/publishing/

| Field | Value |
|---|---|
| PyPI Project Name | `marginalia-ai-plugin-academic-journal` |
| Owner | `John-Cusack` |
| Repository name | `marginalia-plugin-academic-journal` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` |

The PyPI project name does not have to match the GitHub repository name, and here it does
not: the repository keeps its existing name.

## GitHub environments

Create both in **Settings → Environments**, before the first run that needs them:

- **`pypi`** — production uploads. Add required reviewers; this is the gate that makes a
  release deliberate.
- **`testpypi`** — dry runs. Reviewers optional.

## Order

The plugin depends on `marginalia-ai-sdk`, and its integration tests install `marginalia-ai`.
Publish in this order, and only after each artifact installs from the index it was pushed to:

1. `marginalia-ai-sdk`
2. `marginalia-ai`
3. `marginalia-ai-plugin-academic-journal`

Until the first two are on PyPI, `uv lock` cannot resolve this project and CI's integration
and artifact jobs cannot install core.

## Steps

```bash
uv lock                        # commit the lockfile once the dependencies resolve
uv run ruff check acad tests
uv run pytest tests/unit tests/contract -q
uv run pytest tests/integration -q
uv build
uvx --from twine twine check --strict dist/*
```

Then exercise the built wheel in a clean environment with
`scripts/release_smoke.py` (see the README), tag `v0.2.0`, and let the workflow publish.
A manual `workflow_dispatch` run publishes the same artifacts to TestPyPI instead.
