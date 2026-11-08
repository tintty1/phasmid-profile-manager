# Command-line reference

## `phasmid-pm`

Runs the API and the web UI as one process on one port, and opens the UI.

```
usage: phasmid-pm [-h] [--host HOST] [--port PORT] [--no-browser] [--desktop]
                  {user,unlock,leases} ...
```

| Flag | Default | What it does |
| ---- | ------- | ------------ |
| `--host HOST` | `127.0.0.1` (or `PPM_HOST`) | Bind address. Anything other than loopback exposes the API to your network — set `PPM_API_KEY` first. |
| `--port PORT` | `8000` (or `PPM_PORT`) | Port to serve on. |
| `--no-browser` | off | Do not open a browser tab on start. Use when running as a service. |
| `--desktop` | off | Open a native window instead of a browser tab. Needs the `desktop` extra. |
| `-h`, `--help` | | Show the built-in help. |

The flags win over the environment, and the settings the app reports (including
on the Settings screen) reflect what it actually bound to.

```bash
phasmid-pm                          # http://localhost:8000, opens a browser
phasmid-pm --port 9000 --no-browser # another port, no browser tab
phasmid-pm --desktop                # native window
```

To stop the web server, press `Ctrl+C`; in desktop mode close the window.
Any browsers it launched are closed with it.

`python -m phasmid_profile_manager.desktop` is the dedicated desktop launcher. It takes no
flags, picks a free loopback port, and keeps its database and a generated
encryption key in `$XDG_DATA_HOME/phasmid-profile-manager/` (by default
`~/.local/share/phasmid-profile-manager/`). `phasmid-pm --desktop` uses the same
data folder but keeps the `--host` and `--port` flags. Plain `phasmid-pm` and its
administration commands use `data/profiles.db`; set `PPM_DB_PATH` explicitly to
manage the desktop database from the CLI. See
[where data lives](getting-started.md#where-data-lives).

## `phasmid-pm user`

Manages web UI login accounts. **Creating the first account turns login on**:
from then on the API requires a session (or the API key), and the web UI shows
a login screen. Removing the last account turns login back off. Accounts live
in the CLI on purpose — there is no registration endpoint, so creating one
requires shell access to the host, and the first user can be created without a
chicken-and-egg lockout.

```
usage: phasmid-pm user {add,passwd,remove,list} ...
```

| Command | What it does |
| ------- | ------------ |
| `user add <name>` | Create an account. Prompts twice for the password (min 8 characters); it is argon2id-hashed before it touches the database and never appears in argv, the environment, or logs. |
| `user passwd <name>` | Change an account's password. Existing sessions stay valid. |
| `user remove <name>` | Delete an account and its open sessions. Warns when it was the last one. |
| `user list` | List account names and creation times. Never shows hashes. |

`add` and `passwd` take `--password-stdin` to read the password from the first
line of stdin instead of prompting — for provisioning scripts and containers:

```bash
phasmid-pm user add alice                            # interactive
openssl rand -base64 18 | phasmid-pm user add ci-bot --password-stdin
```

The commands work against `PPM_DB_PATH` and take effect immediately, even while
the server is running — the guard checks the database per request.

## `phasmid-pm leases` and `phasmid-pm unlock`

Every browser launch takes a lease on its profile so that two instances sharing
a database cannot open the same one at once (see
[Running more than one instance](../README.md#running-more-than-one-instance)).
These two commands are the operator's view of that.

```bash
phasmid-pm leases                  # id, name, holder, expiry, live or expired
phasmid-pm unlock <profile-id>     # force-release; prompts unless --yes
```

`leases` lists every lease in `PPM_DB_PATH`, including expired ones that nobody
has taken over yet. A holder id is `<hostname>:<pid>:<uuid>`, so it names the
machine and the process to look for.

`unlock` clears a lease whatever it says, and prints who held it. It exists for
the case a lease cannot resolve on its own: a machine that died in a way its
lease could not notice, or one that is verifiably gone and whose TTL you do not
want to wait out. Check `leases` first — force-releasing a lease that is still
live lets a second browser open a profile that is already running, which is the
corruption the lease prevents. There is deliberately no API endpoint for it.

## Installing the browser

There is no CLI command for it. **Install browser** in the web UI (the banner on
the Profiles page, or Settings) and `POST /api/v1/system/browser/install` start
the same background install; see
[Installing the browser](getting-started.md#installing-the-browser). To use a
local Phasmid build and checkout instead, set `PPM_PHASMID_BINARY` and
`PPM_PHASMID_SOURCE`.

Without the browser the app still runs: you can create, edit, group, import and
export profiles. Launching, generating a machine, listing GPUs and placing a
proxy's exit address need it; a profile created before it is installed gets its
machine on its first launch.

## Scripts

These live in `scripts/` and are for working on the project, not for daily use.

| Command | What it does |
| ------- | ------------ |
| `python scripts/build_webui.py` | Builds the Next.js UI as a static export and copies it into the package, so `phasmid-pm` can serve it. Needs Node.js 20.9+. |
| `python scripts/kill_browsers.py` | Force-closes every running process started with `--phasmid-profile` (Phasmid browsers and the proxyroute in front of them), for cleanup after a crash. A normal Chrome is left alone. |
| `python examples/seed_demo.py` | Creates a few demo profiles in `demo_data/profiles.db`. |
| `python scripts/migrate_data.py` | Copies the profiles and groups from `demo_data/` into `data/`. |

## Running from source without the console script

```bash
uv run python -m phasmid_profile_manager.main
```

This starts the API with reload enabled and serves the UI if one has been built.
It reads `PPM_HOST` and `PPM_PORT` and takes no flags — `phasmid-pm` is the
supported entry point.

## Environment variables

Every setting is also an environment variable with the `PPM_` prefix; they are
listed in the [README](../README.md#configuration). A `.env` file in the working
directory is read automatically.

```bash
PPM_PORT=9000 PPM_API_KEY=secret phasmid-pm --no-browser
```
