# Phasmid Profile Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

A self-hosted manager for [Phasmid](https://github.com/tintty1/phasmid) browser
profiles, with a web UI and a REST API on one port.

Phasmid is a Chromium fork that reports the device described by one JSON
profile instead of the machine it runs on. This manager keeps many such
profiles, each with its own browser data and proxy, starts Phasmid for them, and
lets people and agents drive the running browsers over CDP. What Phasmid itself
does is explained in its [README](https://github.com/tintty1/phasmid#readme)
and [docs](https://github.com/tintty1/phasmid/tree/main/docs); this repository
does not repeat it.

This project is based on
[camoufox-profile-manager](https://github.com/polyackiy/camoufox-profile-manager)
by polyackiy (MIT), ported from Camoufox (Firefox) to Phasmid. Profiles, groups,
leases, schedules, backups, Trash, accounts and most of the web UI come from
that project. The browser layer is new.

Status: pre-release. Linux x64 only, because Phasmid ships Linux x64 builds only.

## What it does

- **A profile is one machine.** Its machine is a complete Phasmid profile (OS,
  GPU, screen, fonts, CPU, memory, voices, media devices and the seeds of every
  perturbation), generated once by [phasmid-profile-gen](https://github.com/tintty1/phasmid-profile-gen)
  and stored with the profile. Every launch replays it. You choose the OS
  (Linux or Windows) and optionally a GPU; the rest comes whole from one
  capture of a real device. See
  [docs/profile-settings.md](docs/profile-settings.md).
- **The location follows the proxy.** Every launch looks up the exit address
  through the profile's proxy. When it moved, the timezone, languages,
  geolocation and WebRTC address are regenerated for the new exit from the same
  seed, and the hardware stays exactly as pinned. A proxied launch whose proxy
  does not answer is refused.
- **Profiles and groups**: create, edit, clone, search, filter, bulk actions,
  Trash, and protection for profiles that must not be deleted or given new
  hardware. A clone gets its own machine by default.
- **Browser control**: launch headless, on a virtual VNC display that the web
  UI shows, or as a window on the desktop. Closing the window yourself is
  noticed and the session is cleaned up.
- **Automation over CDP.** Launch returns a `cdp_endpoint`, an authenticated
  WebSocket on the API's port. Playwright's `connect_over_cdp`, Puppeteer and
  other CDP clients attach to the running profile.
- **Proxies**: HTTP, HTTPS and SOCKS5, with or without credentials.
  Authenticated proxies go through Phasmid's `tools/proxyroute`, because Chrome
  cannot send proxy credentials itself. Passwords are encrypted at rest when
  `PPM_SECRET_KEY` is set. *Check proxy* reports where a proxy comes out and
  what a page could notice between that and the profile.
- **Scheduling**: open a profile on a schedule, optionally closing it after N
  minutes. Hardware never changes on a timer. See
  [docs/scheduling.md](docs/scheduling.md).
- **Accounts when you need them.** Open on loopback by default; set
  `PPM_API_KEY` for machine clients, or create a login with
  `phasmid-pm user add <name>`.
- **Moving and keeping profiles**: export a profile with its machine and browser
  data into one archive and import it elsewhere. Automatic backups and Excel
  bulk editing are built in.
- **First-run install**: *Install browser* downloads the newest Phasmid
  release, verifies its checksum, and fetches the matching source tree (for
  proxyroute) and the IP location database.

## Install and run

### From PyPI

Requirements: Linux x64 and [uv](https://docs.astral.sh/uv/) (or pipx).

```bash
uv tool install phasmid-profile-manager   # or: pipx install phasmid-profile-manager
phasmid-profile-manager                   # http://localhost:8000, opens a browser tab
```

This puts `phasmid-profile-manager` (and its short alias `phasmid-pm`) on your
`PATH`, in `~/.local/bin`. Inside any environment where the package is
installed, `python -m phasmid_profile_manager` runs the same command, and
`phasmid-profile-manager serve` is the same as giving no subcommand.

### From source

Requirements: Linux x64, [uv](https://docs.astral.sh/uv/) (it provides Python
3.12+), and Node.js 20.9+ to build the web UI.

```bash
# in a clone of this repository
uv sync
uv run python scripts/build_webui.py
uv run phasmid-pm                   # http://localhost:8000, opens a browser tab
```

Then click **Install browser**, in the banner on the Profiles page or in
Settings. See [Installing the browser](docs/getting-started.md#installing-the-browser)
for what it downloads. To use a local Phasmid build instead, set
`PPM_PHASMID_BINARY` and `PPM_PHASMID_SOURCE` (see [Configuration](#configuration)).

`phasmid-pm` keeps its database in `data/profiles.db` under the working
directory. [docs/cli.md](docs/cli.md) lists the flags and the account and lease
commands.

### With Docker

```bash
docker compose up      # then open http://localhost:8000
```

One container serves the API and the UI on one port, published on loopback
only. The image carries Chrome's runtime libraries, the font families Phasmid's
Linux profiles list, and TigerVNC. It does not contain the browser: click
**Install browser** once, and the release is kept in the `/data` volume with the
database and the profiles.

- Chrome's sandbox needs user namespaces, which Docker's default seccomp profile
  blocks, so `docker-compose.yml` sets `seccomp:unconfined`. The app runs as uid
  1000, because Chrome does not run as root with its sandbox on.
- Every headed launch gets its own VNC display (`PPM_VNC=1` in the image), shown
  in a viewer in the web UI.
- `docker-compose.desktop.yml` opens windows on your Linux Wayland desktop
  instead; see the comments in that file.
- Without a GPU device in the container, the browser runs with `--disable-gpu`
  and renders WebGL on SwiftShader. Pass `/dev/dri` into the container to back a
  hardware GPU claim.

### Desktop window

```bash
uv sync --extra desktop
uv run python scripts/build_webui.py
uv run python -m phasmid_profile_manager.desktop
```

This opens the UI in a native window (pywebview) on a free loopback port, and
keeps its data in `$XDG_DATA_HOME/phasmid-profile-manager/` (by default
`~/.local/share/phasmid-profile-manager/`) with a generated encryption key.
pywebview needs a GUI backend (GTK or Qt) and its system libraries; see
pywebview's installation guide. There is no packaged desktop app yet.

## First steps

1. Click **Install browser** and wait until it says the browser is ready.
2. Click **New profile** and give it a name. Choose Linux or Windows. Leave the
   GPU, languages and timezone unset unless you need them: unset, a GPU is
   drawn from the captures and the location follows the proxy.
3. Add a proxy if you have one, and click **Check proxy**.
4. Press **Run**. The arrow beside it picks headless, virtual display or desktop
   window. The profile runs until you click **Stop** or close its window.

A Windows profile on a Linux host needs a Windows font store, which Phasmid does
not ship. See [Windows profiles](docs/profile-settings.md#windows-profiles).

## Automation over CDP

Every running profile, in every mode, can be driven by a CDP client from another
process. The launch response carries `cdp_endpoint`,
`ws://<host>:<port>/api/v1/profiles/<id>/cdp`: a relay that applies the API's
authentication and passes CDP messages through unchanged.

```python
import httpx
from playwright.async_api import async_playwright

API = "http://127.0.0.1:8000/api/v1"
headers = {"x-api-key": API_KEY} if API_KEY else {}   # only when PPM_API_KEY is set

async with httpx.AsyncClient(base_url=API, headers=headers) as api, async_playwright() as p:
    launch = (await api.post(f"/profiles/{pid}/launch", json={"mode": "headless"})).json()
    browser = await p.chromium.connect_over_cdp(launch["cdp_endpoint"], headers=headers)
    context = browser.contexts[0]          # the profile's own cookies and storage
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://example.com")
    await browser.close()                  # only disconnects; the profile keeps running
    await api.post(f"/profiles/{pid}/close")
```

- Use the profile's existing context. `browser.new_context()` is a blank,
  non-persistent context without the profile's cookies.
- Playwright's `browser.close()` over CDP only disconnects. Stop the browser with
  `POST /api/v1/profiles/{id}/close`.
- Launching a running profile answers `already_running` with the same endpoint,
  so a client can always launch first and connect second.
- The manager starts the browser itself, not through Playwright, so no driver
  switches such as `--enable-automation` are on its command line. How Phasmid
  treats an attached CDP client is in its
  [docs/surfaces.md](https://github.com/tintty1/phasmid/blob/main/docs/surfaces.md#automation).

An agent skill for this lives in
[`skills/phasmid-profile-manager/`](skills/phasmid-profile-manager/SKILL.md).
The API is described in [docs/api.md](docs/api.md#running-browsers).

## Configuration

Settings are environment variables with the prefix `PPM_`. A `.env` file in the
working directory is read too; [`.env.example`](.env.example) is a template.

| Variable | Default | Description |
| --- | --- | --- |
| `PPM_HOST` | `127.0.0.1` | Bind address |
| `PPM_PORT` | `8000` | Port |
| `PPM_DB_PATH` | `data/profiles.db` | SQLite database. Its directory is the data directory: `profiles/`, `run/`, `backups/` and `phasmid/` (the installed browser) live beside it. The desktop window uses `$XDG_DATA_HOME/phasmid-profile-manager/profiles.db` unless this is set |
| `PPM_SECRET_KEY` | *(empty)* | Fernet key; encrypts proxy passwords at rest |
| `PPM_API_KEY` | *(empty)* | When set, required as the `X-API-Key` header (machine clients) |
| `PPM_SESSION_TTL_HOURS` | `168` | Login session lifetime, in hours |
| `PPM_SECURE_COOKIES` | `0` | Force the session cookie's `Secure` flag (set behind a TLS proxy) |
| `PPM_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed origins |
| `PPM_LEASE_TTL` | `120` | Seconds a profile's lease survives without a heartbeat. Minimum 60 |
| `PPM_VNC` | `0` (`1` in Docker) | Headed launches default to their own VNC display instead of a desktop window |
| `PPM_PHASMID_REPO` | `tintty1/phasmid` | GitHub repository *Install browser* takes releases from |
| `PPM_PHASMID_RELEASE` | *(newest)* | Install this release tag instead of the newest one |
| `PPM_PHASMID_BINARY` | *(empty)* | A local Phasmid `chrome` binary to launch instead of the downloaded one |
| `PPM_PHASMID_SOURCE` | *(empty)* | A local Phasmid checkout to run `tools/proxyroute` from |
| `PPM_BACKUP_INTERVAL_HOURS` | `24` | How often each profile is backed up automatically |
| `PPM_BACKUP_RETENTION` | `7` | Routine backups kept per profile |
| `PPM_WEBUI_DIR` | *(auto)* | Serve the web UI from this directory |

phasmid-profile-gen caches the location database under `$XDG_CACHE_HOME/phasmid/geoip`
(by default `~/.cache/phasmid/geoip`). The Docker image sets
`XDG_CACHE_HOME=/data/cache` so the cache stays in the volume.

Generate an encryption key with:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Running more than one instance

Two instances against one database (the web UI and a CLI on the same machine,
or two machines sharing a database file) must not open the same profile at
once: one identity in two browsers means one profile directory written from two
places and the same account live from two addresses.

Each launch therefore takes a **lease** on the profile, and a second instance is
refused (`409` over the API) until the browser closes. The lease is renewed
every 30 seconds while the browser runs and expires `PPM_LEASE_TTL` seconds
after an instance stops renewing it.

```bash
phasmid-pm leases          # who holds what
phasmid-pm unlock <id>     # force-release, after checking the holder is gone
```

Force-release is CLI-only: it is the one operation that can put two browsers on
one identity.

Edits are guarded separately. Each save carries the version it was based on, so
a second save of the same profile is refused and the web UI shows what changed.

Sharing the database file is safe between processes on one machine. Across
machines it needs a filesystem whose locking SQLite can trust, which rules out
most NFS and SMB mounts.

A browser runs in a process session of its own and outlives a manager that is
killed. At startup the manager closes browsers that a dead instance left running
(found by their run directory under `<data dir>/run/`), unless a live instance
holds their lease.

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/getting-started.md](docs/getting-started.md) | Installing, the first profile, backups, where data lives |
| [docs/profile-settings.md](docs/profile-settings.md) | The machine, what follows the proxy, what a launch does, proxies, limitations |
| [docs/api.md](docs/api.md) | The REST API and the CDP relay |
| [docs/cli.md](docs/cli.md) | Every command and flag |
| [docs/scheduling.md](docs/scheduling.md) | Scheduled launches, and why hardware is never rotated on a timer |
| [docs/excel.md](docs/excel.md) | Bulk import and export |
| [docs/releasing.md](docs/releasing.md) | Cutting a release |
| [docs/roadmap.md](docs/roadmap.md) | Known gaps and open items |
| [docs/accessibility-roadmap.md](docs/accessibility-roadmap.md) | What non-technical users still need |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Working on the project |
| [SECURITY.md](SECURITY.md) | Authentication, secrets, reporting a vulnerability |

## Architecture

```
src/phasmid_profile_manager/
├── main.py             FastAPI app; serves the API and the bundled UI
├── cli.py              the phasmid-pm command, including accounts and leases
├── desktop.py          the native-window launcher
├── config.py           environment-based settings
├── core/
│   ├── models.py            profiles, groups, machine settings, schedules
│   ├── database.py          SQLite storage and migrations
│   ├── profile_manager.py   profile lifecycle and the launch sequence
│   ├── machine.py           generating a machine, moving it to a new exit
│   ├── genprofile_driver.py runs phasmid-profile-gen in a child process
│   ├── launch.py            the Phasmid command line and the per-session files
│   ├── browser_session.py   starting, watching and closing browser processes
│   ├── browser_install.py   downloading and verifying a Phasmid release
│   ├── proxy_check.py       where a proxy exits, and whether the profile agrees
│   ├── vnc.py               per-profile Xvnc displays
│   ├── leases.py            cross-instance profile locks
│   ├── scheduler.py         scheduled launches
│   ├── backups.py           automatic and manual backups
│   ├── profile_archive.py   whole-profile export and import
│   ├── excel_manager.py     bulk import and export
│   ├── auth.py              password hashing and login sessions
│   ├── crypto.py            proxy-password encryption at rest
│   └── cleanup.py           finding orphaned profile directories
└── api/                routes (including the CDP and VNC relays), models, errors
web/                    Next.js web interface
skills/                 agent skill for driving the manager
```

Backend: Python, FastAPI, SQLite; Phasmid driven over CDP.
Frontend: Next.js 16, React 19, TypeScript, Tailwind v4.

## Development

```bash
uv sync --extra dev
uv run ruff check src tests
uv run mypy src/phasmid_profile_manager
uv run pytest -m "not browser"    # fast suite, no browser
PPM_PHASMID_BINARY=/path/to/chrome PPM_PHASMID_SOURCE=/path/to/phasmid \
    uv run pytest -m browser --no-network   # real Phasmid launches
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This tool is for lawful use only, for example testing, privacy, and managing
multiple accounts in accordance with each site's terms of service. You are
responsible for how you use it.

## License

[MIT](LICENSE). Based on
[camoufox-profile-manager](https://github.com/polyackiy/camoufox-profile-manager)
by polyackiy, MIT; its copyright notice is kept in [LICENSE](LICENSE).

Phasmid is a separate project under the BSD 3-Clause licence, downloaded at run
time and not distributed with this one. Profiles are generated by
phasmid-profile-gen (BSD 3-Clause), a dependency. The location data it uses
keeps its own licences: IP Geolocation by [DB-IP](https://db-ip.com) (CC BY
4.0), and timezone boundaries from timezone-boundary-builder via
timezonefinder-data (ODbL 1.0, © OpenStreetMap contributors). See
[its README](https://github.com/tintty1/phasmid-profile-gen#licence).
