# Contributing

Thanks for your interest in improving Phasmid Profile Manager. This document
describes how to set up a development environment and the conventions we follow.

Problems with what the browser itself reports belong to
[Phasmid](https://github.com/tintty1/phasmid), not here.

## Development setup

The backend uses [uv](https://docs.astral.sh/uv/) and needs Python 3.12 or
newer, because Phasmid's genprofile does. It runs on Linux x64 only.

```bash
uv sync --extra dev
```

For the browser tests you need a Phasmid build and a checkout of the same
version, and the DB-IP database genprofile caches. The simplest way to get all
three is to run the app once and click **Install browser**; the release is then
in `data/phasmid/<tag>/browser/chrome` and `data/phasmid/<tag>/source`.

The frontend is a Next.js app in `web/`. It needs Node.js 20.19+ (ESLint 10's
floor); building the bundle alone works from 20.9:

```bash
cd web
npm install
npm run dev
```

## Running checks

Please make sure the following pass before opening a pull request:

```bash
# Backend
uv run ruff check src tests            # lint
uv run ruff format --check src tests   # formatting
uv run mypy src/phasmid_pm             # type-check
uv run pytest -m "not browser"         # fast tests, no browser

# Opt-in: real Phasmid launches
PPM_PHASMID_BINARY=/path/to/chrome PPM_PHASMID_SOURCE=/path/to/phasmid \
    uv run pytest -m browser --no-network

# Frontend
cd web && npm run lint && npm run build
```

CI runs the backend checks on Python 3.12 and 3.13, and the frontend lint, build,
static export and UI smoke tests (`web/tests/stage_one_ui.py`).

To work on the single-process mode (`phasmid-pm`), build the UI into the package
first, otherwise the server has nothing to serve:

```bash
uv run python scripts/build_webui.py
```

## Testing conventions

- **`-m browser` means the test needs a real Phasmid**, not merely that it is
  slow. The fast suite replaces the browser with a fake executable and genprofile
  with a fake generator (`tests/fakes.py`, `tests/conftest.py`), and answers the
  exit-address lookup locally. Anything that needs the real binary or the real
  genprofile belongs under `-m browser`; CI does not install Phasmid, so an
  unmarked test that needs it fails there while passing on your machine. Browser
  tests skip themselves unless `PPM_PHASMID_BINARY` and `PPM_PHASMID_SOURCE` are
  set and the DB-IP database is cached.
- **The browser suite runs offline. Keep it that way.** Pages come from a
  loopback server ([tests/browser/support.py](tests/browser/support.py)), and the
  exit address a launch would look up is answered locally. A new test that
  reaches the internet would make the suite fail on someone else's rate limit;
  if one truly needs to, say why in the test. `--no-network` refuses any
  connection off this machine from the test process:

  ```bash
  uv run pytest -m browser --no-network
  ```

  It guards the test process only. The browser is a child process of its own,
  and what keeps its page loads local is that the tests only ever hand it
  loopback URLs.
- **Prefer tests that observe real behaviour.** The browser tests launch Phasmid
  and read what a page actually sees (platform, cores, memory, languages,
  screen, timezone, `navigator.webdriver`), rather than asserting on the
  command line passed in.
- **A test should be able to fail.** Coverage is a way of finding untested
  behaviour, not a target. Name the behaviour in the test name, and when the
  test exists because something once broke, say in a docstring what broke.

### What is deliberately not covered

- **`desktop.py`** composes uvicorn and pywebview and has little logic of its
  own. pywebview is an optional extra that CI does not install, and the thing
  that can actually break here, the window opening at all, is not observable
  without a display.
- **The FastAPI lifespan in `main.py`** builds the storage and profile manager
  from the settings on startup. The integration tests inject their own instead,
  which is what lets each of them have a throwaway database.
- **`core/genprofile_driver.py`** runs inside the Phasmid source tree, where
  genprofile is importable. It is excluded from mypy, the unit tests replace it
  with a stand-in script, and only the browser tests run it for real.

Most remaining uncovered lines are `except Exception -> 500` handlers, which
exist for failures that cannot be provoked without breaking SQLite underneath
the process.

## Project layout

- `src/phasmid_pm/`: the Python package (core logic, REST API, CDP and VNC
  relays).
- `web/`: the Next.js web interface.
- `tests/`: unit, integration, and opt-in browser tests.
- `skills/`: the agent skill and its helper script.
- `scripts/`, `examples/`: utilities and runnable examples.

## Coding conventions

- **Style:** [ruff](https://docs.astral.sh/ruff/) handles linting and formatting.
- **Types:** new code should type-check under mypy.
- **Language:** all code, comments, docstrings, and UI text are in English.
- **Commits:** follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, …).

## Pull requests

1. Create a feature branch.
2. Make your change with tests where it makes sense.
3. Run the checks above.
4. Open a pull request describing the change and its motivation.

## Reporting bugs

Open an issue using the bug-report template. Include the version, the Phasmid
release (Settings shows it), your Linux distribution, and steps to reproduce.
