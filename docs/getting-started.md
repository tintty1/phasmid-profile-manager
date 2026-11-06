# Getting started

## What you need

- A Linux x64 computer. Phasmid ships Linux x64 builds only, so the manager runs
  only there (or in Docker on a Linux x64 host).
- Internet access for the first browser install.
- To run from source: [uv](https://docs.astral.sh/uv/), which provides Python
  3.12 or newer, and Node.js 20.9+ to build the web UI. Or Docker.

There is no packaged desktop app yet.

## Start the manager

From a clone of this repository, either in a browser tab:

```bash
uv sync
uv run python scripts/build_webui.py
uv run phasmid-pm               # opens http://localhost:8000
```

or in a native window:

```bash
uv sync --extra desktop
uv run python scripts/build_webui.py
uv run python -m phasmid_pm.desktop
```

The window needs a pywebview GUI backend (GTK or Qt) and its system libraries;
see pywebview's installation guide.

or in Docker:

```bash
docker compose up               # then open http://localhost:8000
```

The three keep their data in different places; see
[where data lives](#where-data-lives).

## Installing the browser

The manager does not include Phasmid. Click **Install browser** in the banner on
the Profiles page (it is also in Settings). Progress and errors appear in the
app, with **Retry download** if something fails. The install:

- finds the newest release of [tintty1/phasmid](https://github.com/tintty1/phasmid/releases)
  that has a Linux x64 build (pre-releases count), downloads `phasmid-<tag>-linux-x64.tar.zst`, checks it
  against the `.sha256` published beside it, and unpacks it;
- downloads Phasmid's source tree at the same tag, for `tools/proxyroute`
  (which carries proxy credentials Chrome cannot send);
- has phasmid-profile-gen, which generates every profile's machine, download
  the DB-IP City Lite database it places exit addresses with, about 60 MB.

The browser and source go into `phasmid/<tag>/` in the data folder; the database
goes into phasmid-profile-gen's cache, `$XDG_CACHE_HOME/phasmid/geoip` (by default
`~/.cache/phasmid/geoip`). An interrupted install leaves the previous one in
use. `PPM_PHASMID_RELEASE` installs a given tag; `PPM_PHASMID_BINARY` and
`PPM_PHASMID_SOURCE` use a local build and checkout instead.

## First profile

1. Choose **Create first profile** or **New profile**, and give it a name you
   recognise.
2. Choose Linux or Windows. Leave the advanced settings (GPU, languages,
   timezone, WebRTC policy) at their defaults: a GPU is drawn from captured
   devices, and the location follows the proxy.
3. Add a proxy if you have one, and click **Check proxy** before relying on it.
   It shows where the proxy comes out and anything a page could notice.
4. Click **Run**. The arrow beside it chooses **Headless**, **Virtual display**
   (watch and use it in the manager) or **Desktop window**. Close the window or
   click **Stop** when finished. Closing the manager also stops its browsers.

The profile keeps its cookies, history and machine between launches. Every
launch places it at the proxy's current exit address, with the same hardware. A
clone is a different machine by default. *Regenerate fingerprint* gives a profile
different hardware; read the confirmation before doing it. A Windows profile
needs a Windows font store on this computer; see
[Windows profiles](profile-settings.md#windows-profiles).

## Protect and recover your data

**Settings → Profile backups → Back up profiles** creates a recovery archive.
Stop your profiles first; running or leased profiles are skipped and retried
later. Keep a copy outside this computer. **Restore as new profile** creates a
separate profile, so the current one is kept.

Deleting a profile moves it to **Trash**. **Restore** there brings it back.
**Delete permanently** asks you to type its name and removes its browser data;
after that only an earlier backup can bring it back.

Protect backup archives as carefully as the profiles: they are unencrypted and
can contain signed-in sessions, cookies, history and proxy passwords. They do not
contain the manager's login accounts, its sessions, or its settings. Restoring
an archive does not need the original encryption key; imported proxy passwords
are encrypted with the current key. Keep the original database and key to
preserve a whole installation.

Automatic backups run every 24 hours by default, keeping seven routine archives
and two before-update checkpoints per profile. Trash has no automatic expiry;
restoring keeps the profile's schedules paused.

**Settings → App updates → Check for updates** checks this project's GitHub
releases. **Back up and prepare update** backs up every profile first and then
links to the release; installing it is up to you. The app never replaces itself.

## Where data lives

The data folder is the directory of the database (`PPM_DB_PATH`):

| Started with | Data folder |
| --- | --- |
| `phasmid-pm` | `data/` under the working directory |
| `python -m phasmid_pm.desktop` or `phasmid-pm --desktop` | `$XDG_DATA_HOME/phasmid-profile-manager/`, by default `~/.local/share/phasmid-profile-manager/` |
| Docker | the `ppm-data` volume, mounted at `/data` |

It holds `profiles.db`, each profile's browser data in `profiles/`, `backups/`,
the installed browser in `phasmid/`, and per-session files in `run/` while
browsers are open. The desktop launcher also keeps `secret.key` there, an
encryption key it generates for a fresh data folder; losing it makes the
encrypted proxy passwords unreadable. A CLI installation keeps using
`data/profiles.db` unless configured otherwise; its data is not moved into the
desktop folder.

The desktop window's server listens on this computer only, on a free port. If it
says **already open**, switch to its existing window. `desktop.log` in the data
folder records output when no console is attached. Do not share logs or backups
publicly without checking them for private information.

[CLI options](cli.md) and [profile settings](profile-settings.md) cover advanced
use.
