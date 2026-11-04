# Releasing

A release is a Python wheel and sdist with the web UI built in. There are no
desktop installers and nothing is signed. The Phasmid browser is not part of a
release: the running app installs it from Phasmid's own releases.

A `v*` tag triggers `.github/workflows/release.yml`, which:

1. builds the web UI into the package (`scripts/build_webui.py`);
2. builds the wheel (`uv build --wheel`, which includes the bundled UI) and the
   sdist (`uv build --sdist`, source only);
3. writes a `.sha256` beside each;
4. attaches all of them to a **draft** GitHub Release with generated notes,
   marked as a pre-release when the tag contains `-`, and never as Latest;
5. optionally publishes to PyPI (below).

A maintainer publishes the draft after checking it.

## Before tagging

1. Update `CHANGELOG.md`, `pyproject.toml` and `web/package.json` (and
   `web/package-lock.json` when the npm version changes) to the same version.
   Pre-releases use PEP 440 `0.2.0rc1` in Python and SemVer `0.2.0-rc.1` in npm
   and the tag.
2. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md): ruff, mypy, the fast
   test suite, and the frontend lint and static build.
3. Run the browser suite against the Phasmid release the app currently installs
   (`PPM_PHASMID_BINARY` and `PPM_PHASMID_SOURCE` pointing at its unpacked
   `browser/chrome` and `source/`):
   `uv run pytest -m browser --no-network`.
4. Build and install the wheel in a clean environment, run
   `phasmid-pm --no-browser`, and confirm `/health` reports the new version.
   With an empty data directory, install the browser from Settings, create a
   profile, launch it in each available mode, and connect to its `cdp_endpoint`
   with Playwright. Check backups and restore, Trash, and that closing the
   manager closes its browsers.
5. Build the Docker image from the tag and repeat the launch check there.
6. Create and push an annotated `v<version>` tag. Watch **Release** finish, check
   that every asset and its checksum are present, then publish the draft.

## Update boundaries

Settings checks this project's GitHub repository for its latest stable release
(a `vX.Y.Z` tag that is neither a draft nor a pre-release). Update preparation
backs up every profile first and then links to the release. The app does not
download or install the update, verify signatures, or replace itself.

## Publishing to PyPI (optional)

PyPI publishing is off by default. Set the repository variable
`PUBLISH_TO_PYPI=true`, create a GitHub environment named `pypi`, and configure
a PyPI Trusted Publisher for this repository, workflow `release.yml` and
environment `pypi`. The workflow uses OIDC, not an API token, and skips
pre-release tags.
