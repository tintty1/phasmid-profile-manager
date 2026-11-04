# REST API reference

The API is the one the web UI uses. A running instance serves interactive
documentation at **`/docs`** (Swagger) and **`/redoc`**, generated from the code;
that is the authoritative schema. This page is the guided tour, and the
[stability contract](#stability-contract) below is what the project intends to
keep.

Base URL: `http://127.0.0.1:8000` by default. Endpoints live under **`/api/v1`**.

The unversioned `/api/...` paths, kept from the upstream project, work as
aliases (same routes, same behaviour) but are left out of the OpenAPI schema so
a generated client sees each operation once. New code should use `/api/v1`.

## Authentication

Two independent mechanisms, either of which satisfies the guard on every
`/api/...` route. Which ones are active follows from what is configured; there
is no separate switch:

1. **Nothing configured** (no users, no `PPM_API_KEY`): the API is open. This is
   the default, and it is acceptable because the app binds to loopback.
2. **`PPM_API_KEY` set**: every request must carry the key:

   ```bash
   curl -H "X-API-Key: $PPM_API_KEY" http://localhost:8000/api/v1/profiles
   ```

   The key is compared in constant time. The web UI can send it too: paste it
   into the Settings screen, which stores it in that browser only.
3. **Any user account exists** (`phasmid-pm user add <name>`): the API requires
   a login session, or the API key, so machine clients keep working once a human
   account is created. The web UI shows a login screen; other clients call
   `POST /api/v1/auth/login` and carry the `cpm_session` cookie it sets. There
   is no registration endpoint: accounts are created from the CLI.

**Configure at least one of them before binding to anything other than
`127.0.0.1`.** Without either, anybody who can reach the port can read your
profiles, including proxy passwords, and drive their browsers.

The WebSocket relays (`/profiles/{id}/cdp`, `/profiles/{id}/vnc`) apply the same
rules themselves: a session cookie, an `x-api-key` header, or the key as a
`ppm-key.<base64url>` subprotocol (for browsers, which cannot set headers on a
WebSocket). They also refuse an `Origin` that is neither in `PPM_CORS_ORIGINS`
nor this host.

### Login sessions

```http
POST /api/v1/auth/login       {"username": "...", "password": "..."}
POST /api/v1/auth/logout
GET  /api/v1/auth/session
```

These three are the only unauthenticated API routes: login has to work logged
out, and `GET /auth/session` is how the UI decides whether to show the login
screen. Login and `GET /auth/session` answer
`{user_auth_enabled, authenticated, username}`; login also sets the session
cookie. Logout returns the action envelope and clears the cookie.

- A successful login sets `cpm_session`: **HttpOnly**, **SameSite=Lax**,
  `Path=/`, and **Secure** when the request came over HTTPS or
  `PPM_SECURE_COOKIES=1`. It expires after `PPM_SESSION_TTL_HOURS` (default
  168, one week).
- The session is server-side: the database stores only a SHA-256 of the token,
  and logout deletes the row.
- A failed login is a `401` with the same body whether the username exists or
  the password is wrong, costs an argon2 verification either way, and is
  delayed half a second.
- Passwords are hashed with argon2id; no response or log carries a password or
  a hash.

## Conventions

- Request and response bodies are JSON, except file upload and download.
- Resource endpoints return the resource itself. Action and system endpoints
  that have no resource to return use the envelope
  `{"success": bool, "message": str, "data": ...}`.
- **A field you send is authoritative, including `null`, which clears it. A field
  you omit is left alone.** This matters most on `PUT /api/v1/profiles/{id}`:
  send `{"proxy_config": null}` to detach a proxy; omit the key to leave it.
- Statuses: `400` for a bad request, `401` for missing or wrong credentials,
  `404` for a missing resource, `405` for a method the path does not accept,
  `409` for a state conflict, `422` for values that fail validation, `500`
  otherwise. `409` carries a code that says which conflict: `stale_write` when
  someone else saved the profile first, `conflict` for a profile that is running
  or leased elsewhere, `protected` for a protected profile asked to be deleted
  or given new hardware.

### Errors

Every non-2xx response from an `/api` route has one shape (`/health` is the
exception; see [System](#system)):

```json
{
  "error": {
    "code": "not_found",
    "message": "Profile with ID x1 not found",
    "details": null
  },
  "detail": "Profile with ID x1 not found"
}
```

- `error.code` is a stable snake_case token: `bad_request`, `unauthorized`,
  `not_found`, `method_not_allowed`, `conflict`, `stale_write`, `protected`,
  `payload_too_large`, `validation_error`, `internal_error`, `unavailable`.
- `error.message` is always a single human-readable string, including for
  validation failures.
- `error.details` carries the structured specifics when there are any; for
  validation failures it is pydantic's error list, each entry with `loc`, `msg`
  and `type`.
- `detail` mirrors `error.message` for clients written against FastAPI's
  default shape. It is deprecated; read `error.message`.

## Stability contract

`1.0` is meant to freeze, for the whole `1.x` line:

- the `/api/v1` paths, methods and status codes documented here;
- the names and types of every documented request and response field (fields
  are only ever added, never renamed or removed);
- the error shape above and its `error.code` values;
- the pagination shape on `GET /api/v1/profiles`.

New endpoints, new optional parameters and new response fields are not breaking
changes and can appear in any release.

Deliberately **outside** the contract:

- the contents of the `fingerprint` summary on a profile, and the machine JSON
  from `GET /profiles/{id}/machine`: they come from the Phasmid profile and
  change when Phasmid's schema does;
- `launch` in the launch response: launch internals, exposed for debugging;
- the entries of `actions` in profile statistics;
- the fields of a GPU entry beyond `os` and `gpu`.

### Deprecated

| Deprecated | Use instead |
| --- | --- |
| Top-level `detail` in error responses | `error.message` |
| `window_size` in the launch request | nothing: it is ignored, the window is the profile's own size |

The unversioned `/api/...` aliases stay for the whole `1.x` line.

## Profiles

### List

```http
GET /api/v1/profiles?page=1&per_page=25&status=active&group=<id>&search=shop
```

Returns `{profiles, total, page, per_page, has_next, has_prev}`. `per_page` is
1-100 (default 10); `search` matches the name. This is the only paginated
collection.

### Create

```http
POST /api/v1/profiles
```

```json
{
  "name": "shop-de-01",
  "group": "<group id>",
  "notes": "free text",
  "protected": false,
  "generate_fingerprint": true,
  "browser_settings": {
    "os": "windows",
    "gpu": "0x10de:0x2504",
    "languages": ["de-DE", "de", "en-US", "en"],
    "timezone": null,
    "webrtc_policy": "default"
  },
  "proxy_config": {
    "type": "socks5", "server": "host:1080",
    "username": "u", "password": "p"
  }
}
```

Only `name` is required. `browser_settings` is described in
[profile-settings.md](profile-settings.md#settings): `os` is `linux` (default)
or `windows`; `gpu` is a `<vendor_id>:<device_id>` from
[`GET /fingerprints/gpus`](#gpus); `languages` and `timezone` unset follow the
proxy's exit address; `webrtc_policy` is one of `default`,
`default_public_interface_only`, `default_public_and_private_interfaces`,
`disable_non_proxied_udp`. An unknown timezone or a malformed `gpu` is refused
with `400` here (and with `422` on update).

With `generate_fingerprint` (the default) and the browser installed, the machine
is generated now; otherwise the first launch generates it. Responds `201` with
the profile. Its `fingerprint` is a summary of the machine, or `null` until one
is generated.

### Read, update, delete

```http
GET    /api/v1/profiles/{id}
PUT    /api/v1/profiles/{id}
DELETE /api/v1/profiles/{id}
GET    /api/v1/profiles/{id}/machine
```

`GET .../machine` returns the whole stored Phasmid profile JSON, as the next
launch would hand it to the browser apart from a location move; `404` until a
machine is generated.

`PUT` takes `name`, `group`, `status` (`active`, `inactive`, `blocked`,
`maintenance`), `notes`, `protected`, `browser_settings`, `proxy_config` and
`row_version`. `browser_settings` is **merged** over what is stored, so sending
one field does not reset the others. Changing `os` or `gpu` on a profile that
has a machine takes effect only when a new machine is generated; an OS that
disagrees with the machine is reported as `fingerprint.os_mismatch`. Changing
the proxy clears the stored proxy check.

`DELETE` moves the profile to Trash, keeps its browser directory and pauses its
schedules. A trashed profile is hidden from normal list, get and launch calls. A
running or leased profile returns `409`. Use the [Trash endpoints](#recovery-and-first-run-setup)
to erase it.

#### Protected profiles

Create or update a profile with `"protected": true` to guard an account that
matters against mistakes, such as a misdirected script or an agent. While it is
set, moving the profile to Trash, purging it, `reset-fingerprint` and
`reconcile-os` with `keep_machine: false` are refused with **`409`** and the code
`protected`. Editing, launching and cloning are unaffected, and a clone starts
unprotected. Send `{"protected": false}` to lift it.

#### Not overwriting someone else's save

Every profile carries a `row_version`, bumped by each save. Send back the one you
read, and a save that landed in between is refused with **`409`** and the code
`stale_write` instead of being silently reverted:

```http
PUT /api/v1/profiles/{id}    {"notes": "...", "row_version": 7}
```

Reload the profile and retry against the `row_version` that comes back. The
refusal writes nothing.

Omitting `row_version` is supported: the server then guards only against two
requests overlapping, which cannot catch an edit made against a form loaded
minutes ago.

A proxy check and a browser launch write only the columns they own
(`proxy_check`; the machine and `last_used`) and do not bump the version, so a
check or a launch does not make every open edit form fail to save.

### Other actions

```http
POST /api/v1/profiles/{id}/clone              {"new_name": "...", "regenerate_fingerprint": true, "copy_data": true}
POST /api/v1/profiles/{id}/reset-fingerprint
GET  /api/v1/profiles/{id}/stats
```

A clone copies the settings and proxy, and makes two independent choices:

- `copy_data` (default `true`) copies the browser data: cookies, logins, Local
  Storage, IndexedDB, history, saved passwords. With `false` the clone starts as
  a fresh browser, logged out everywhere. Caches and downloads are never copied.
- `regenerate_fingerprint` (default `true`) gives the clone **its own machine**.
  Two profiles sharing a machine report the same GPU, screen, cores and seeds,
  which is provably one computer.

The clone starts unprotected and unchecked.

`reset-fingerprint` generates a new machine with a new seed. Without an
installed browser the machine is dropped and the next launch generates one.

```http
POST /api/v1/profiles/{id}/reconcile-os     {"keep_machine": true}
```

Brings a profile's `os` setting and its machine back into agreement. The summary
reports the disagreement as `pinned_os`, `settings_os` and `os_mismatch`.

- `keep_machine: true` sets `os` to what the machine reports and changes nothing
  else.
- `keep_machine: false` generates a new machine for the `os` setting: new seed,
  hardware, screen, GPU and fonts.

`400` when the profile has no machine, when its OS cannot be read, when the two
already agree, or (for a new machine) when the browser is not installed.

```http
POST /api/v1/profiles/clear-geography    {"profile_ids": ["a1b2c3d4", ...]}
```

Unsets the stated `timezone` of each profile named, so its location follows the
proxy again; languages are not touched. Returns `cleared`, `unchanged` (no
timezone was stated) and `not_found`; `422` for an empty list. The next launch
of each cleared profile places its machine at the exit again.

## GPUs

```http
GET /api/v1/fingerprints/gpus?os=windows
```

The GPUs a generated machine may claim: those captured on a real host, and
those the captured host class backs with the same WebGL surface (genprofile's
claims). Wrapped in the action envelope: `data.gpus`, each entry
`{os, gpu, renderer, weight}`, and `data.total`. Pass `gpu` as
`browser_settings.gpu`. `weight` is how often it is drawn when no GPU is chosen.
The list is empty until the browser is installed.

## Checking a proxy

```http
POST /api/v1/profiles/{id}/check-proxy
POST /api/v1/proxy/check      {"proxy_config": {...}, "browser_settings": {...}}
```

Both reach the internet through the proxy and return the same shape:
`reachable`, `error`, `latency_ms`, the `location` it comes out at (`ip`,
`country`, `timezone`, `latitude`, `longitude`) and `findings`, each with a
`level` of `error`, `warning` or `info`, the `field` it concerns and a message.
The location is placed by genprofile with DB-IP City Lite, so it needs the
browser installed; without it, a working proxy is reported with only its `ip`
and a note.

A proxy that does not answer is reported with `reachable: false` and an `error`,
not as a 5xx. The second form takes an unsaved proxy, so a profile can be checked
before it exists; omit `proxy_config` to check the direct connection.

Checking a **saved** profile keeps the answer on it as `proxy_check`
(`checked_at`, `reachable`, `error`, `latency_ms`, `ip`, `country`, `timezone`,
`findings`), and returns `checked_at` on the check response. It is `null` until
the profile is checked, and again whenever its proxy changes. Nothing writes it
but a check that was asked for.

## Running browsers

```http
POST /api/v1/profiles/{id}/launch    {"mode": "virtual"}
POST /api/v1/profiles/{id}/close
GET  /api/v1/browsers/active
GET  /api/v1/browsers/modes
POST /api/v1/browsers/close-all
WS   /api/v1/profiles/{id}/cdp
```

The launch request takes `mode`, `headless` (the older way to ask for
headless), and `args`, a list of extra Chrome switches appended to the command
line. Anything a page can observe should come from the profile, not from `args`.

Launch responds with `status` (`launched`, or `already_running` if the profile's
browser is already up), a `message`, the `process_id`, the `mode`, the
`cdp_endpoint`, and `launch`: the command line without the proxy-arguments
file, the `exit_ip` the launch found, and whether the machine `moved`. Closing a
browser that is not running is reported as `status: "not_running"`, not as an
error.

What a launch does, step by step, is in
[profile-settings.md](profile-settings.md#what-a-launch-does). Its failures:

| Status | When |
| --- | --- |
| `400` | genprofile could not generate or move the machine (for example an exit address DB-IP cannot place) |
| `404` | No such profile |
| `409` | Another instance holds the profile's lease |
| `500` | The browser is not installed; a proxied launch whose proxy did not answer; the mode is unavailable here; the browser exited during startup or did not open its CDP port within 45 seconds. The message says which |

`mode` is where the browser draws, chosen per launch:

| `mode` | Draws on |
| --- | --- |
| `headless` | nothing (`--headless`) |
| `virtual` | its own VNC display, viewable in the web UI (needs TigerVNC's Xvnc) |
| `window` | this machine's desktop; in Docker, `docker-compose.desktop.yml` |

Left out, `headless` decides, and a headed launch uses `virtual` when `PPM_VNC`
is on (the Docker image) and `window` otherwise. `GET /api/v1/browsers/modes`
returns `{default, modes: [{mode, available, reason}]}`, so a client can see
which modes this instance supports; the web UI's Run menu is built from it.

`GET /api/v1/browsers/active` returns `{active_browsers, count}`, each entry
`{profile_id, process_id, started_at, mode, vnc, cdp}`. It lists this
instance's browsers only.

### Driving a browser over CDP

Every running browser, in every mode, can be driven by a CDP client from another
process. Connect to the `cdp_endpoint` from the launch response,
`ws://<host>:<port>/api/v1/profiles/<id>/cdp`, built from the URL the launch
request used:

```python
browser = await playwright.chromium.connect_over_cdp(
    cdp_endpoint,                      # ws://127.0.0.1:8000/api/v1/profiles/<id>/cdp
    headers={"x-api-key": API_KEY},    # only when PPM_API_KEY is set
)
context = browser.contexts[0]          # the profile's own context: its cookies and storage
page = context.pages[0] if context.pages else await context.new_page()
```

or `puppeteer.connect({browserWSEndpoint: cdp_endpoint, headers: {...}})`. CDP
is Chrome's own protocol, so there is no client/server version lock.

The browser listens for CDP on a loopback port of its own
(`--remote-debugging-port=0`) with no authentication. The relay is the way in
from outside: it applies the API's authentication, then passes messages through
untouched, so it works unchanged from Docker. It forwards the browser-level
target only; HTTP discovery endpoints such as `/json/version` are not served.
The relay closes the socket with `4401` (authentication), `4403` (origin),
`4404` (the profile has no running browser) or `1011` (the browser refused the
connection).

- Playwright's `browser.close()` on a CDP connection only disconnects; the
  profile keeps running. `POST /profiles/{id}/close` stops it, as does CDP
  `Browser.close`.
- Launching an already-running profile returns the same endpoint, so an agent
  can always launch first and connect second.
- Several clients can attach to one running profile at once and share its
  pages.

Closing the browser window yourself is detected and the session is cleaned up,
so `/api/v1/browsers/active` reflects reality without polling.

### Downloads

Each profile downloads into `<data dir>/downloads/<profile id>/` on the server
(`/data/downloads/...` in Docker, inside the volume), not into `~/Downloads`:

```http
GET    /api/v1/profiles/{id}/downloads[?guid=]  {"profile_id", "downloads_dir", "files": [{"name", "size", "modified", "complete", "guid"}]}
GET    /api/v1/profiles/{id}/downloads/{name}   the file, as an attachment
DELETE /api/v1/profiles/{id}/downloads/{name}
```

Files are listed newest first; `complete` is false while the browser is still
writing one. `modified` carries a UTC offset. A name is the bare file name; anything that would leave the
folder is a `404`. The folder works whether or not the browser runs, is not part
of exports, backups or clones, and is deleted when the profile is purged.

Downloads a person starts go there through the profile's
`download.default_directory` preference, written before every launch; Chrome
names them, adding ` (1)` to a taken name.

Downloads a CDP client starts are handled by the relay. A client chooses the
download folder with `Browser.setDownloadBehavior`; Playwright's
`connect_over_cdp` sends one on connect, naming a temporary folder **on the
client's machine**, which does not exist when the browser is in Docker or on
another host (its `download.save_as()` then writes an empty file). The relay
rewrites that call: downloads go to `<downloads>/.incoming/` named by GUID,
with events on, and when the browser reports one complete the relay moves it
into the folder under its own name, never overwriting (`export (1).csv`). So
fetch files through the API, not `save_as()`:

- `GET /profiles/{id}/downloads?guid=<guid>` finds one download by the
  browser's GUID, which Playwright shows as the file name of
  `download.path()`; names can repeat, GUIDs cannot. A download still running
  is listed with `complete: false`.
- `behavior: deny` and `default` pass through unchanged. When the last client
  disconnects, the relay sets `default` again, so the preference applies.
- A client connecting to Chrome's own loopback port instead of the relay gets
  none of this.

Launching a profile that another instance has open returns **`409`**. Retry once
the holder closes the browser, or wait out `PPM_LEASE_TTL` if that instance is
gone. There is no force-unlock endpoint: releasing another instance's lease
needs `phasmid-pm unlock` on the host.

## Recovery and first-run setup

```http
GET    /api/v1/trash/profiles
POST   /api/v1/trash/profiles/{id}/restore
DELETE /api/v1/trash/profiles/{id}?confirm=true
GET    /api/v1/system/backups
POST   /api/v1/system/backups             {"profile_ids": ["id"], "reason": "manual"}
POST   /api/v1/system/backups/{id}/restore {"name": "Recovered profile"}
GET    /api/v1/system/browser
POST   /api/v1/system/browser/install
GET    /api/v1/system/updates
POST   /api/v1/system/updates/prepare
```

These use the same authentication as the rest of the API and return
`{success, message, data}`.

Trash has no automatic expiry. Restoring a profile reuses its id but leaves its
schedules paused. Permanent deletion requires `confirm=true` and only accepts
trashed, closed, unprotected profiles.

Backups carry the settings, the machine and the browser data. Restore creates a
**new** profile, leaving existing ones unchanged. Archives contain session
cookies and decrypted proxy credentials. They do not include app accounts,
settings or schedules. Backup creation returns `data.backups` and `data.skipped`
(profile id and reason); running profiles, missing directories and unreadable
credentials are reported as skipped. Omit `profile_ids` to back up all profiles,
including Trash. The running app checks for due backups every minute, by default
one snapshot per 24 hours and seven routine snapshots per profile
(`PPM_BACKUP_INTERVAL_HOURS`, `PPM_BACKUP_RETENTION`). The two latest
before-update checkpoints are kept separately.

Browser status is `{installed, version, state, progress, message, error}`, where
`state` is `idle`, `downloading`, `ready` or `error`, and `progress` is 0-100 or
null. `installed` is true only when the binary, the source tree with
`tools/genprofile` and `tools/proxyroute`, and the location database are all
present. GET downloads nothing. POST starts a background install in this
process, and repeated POSTs while it runs reuse it; poll GET to see it finish.
The install:

1. finds the newest non-draft release of `PPM_PHASMID_REPO` (pre-releases count)
   that has `phasmid-<tag>-linux-x64.tar.zst` and its `.sha256`, or the tag in
   `PPM_PHASMID_RELEASE`;
2. downloads the archive, checks its SHA-256 and unpacks it into
   `<data dir>/phasmid/<tag>/browser`;
3. downloads the source tree at the tag from GitHub into
   `<data dir>/phasmid/<tag>/source`;
4. has genprofile download the DB-IP City Lite build it pins (about 60 MB,
   127 MB unpacked) into `$XDG_CACHE_HOME/phasmid/geoip`;
5. writes `<data dir>/phasmid/current` last, so an interrupted install leaves the
   previous one in use.

Parts already present are skipped. With `PPM_PHASMID_BINARY` and
`PPM_PHASMID_SOURCE` both set, only the location database is downloaded. One of
them alone is completed from the downloaded release. An install holds a file
lock, so two managers sharing a data directory do not install at once.

The update check asks this project's GitHub repository for its latest stable
release and returns
`{current_version, latest_version, available, release_url, assets, error}`.
Prepare creates before-update backups and returns
`{release_url, backup_count, message}` only if every profile was backed up. It
fails with `409` for incomplete backups or no newer release, and `503` when the
release check failed. It does not replace the running application.

### Manual profile transfer

```http
GET  /api/v1/profiles/{id}/export      → application/zip
POST /api/v1/profiles/import[?name=…]  multipart: file=<archive.zip>[, name=…]
```

The archive carries the profile, its machine and its browser data (cookies,
storage, history, saved logins), without Chrome's caches. Import assigns a new
id and drops the group and the last proxy check.

- Exporting a **running** profile, or one leased by another instance, returns
  `409`.
- The archive is **not encrypted** and holds live session cookies and the proxy
  password.

## Schedules

```http
GET    /api/v1/schedules
POST   /api/v1/schedules
GET    /api/v1/schedules/{id}
PUT    /api/v1/schedules/{id}
DELETE /api/v1/schedules/{id}
POST   /api/v1/schedules/{id}/run
GET    /api/v1/schedules/{id}/runs
```

A schedule has one action, `launch`: open the profile's browser, and with
`run_minutes` close it again that many minutes later. Regenerating the machine is
deliberately not schedulable; see [scheduling.md](scheduling.md).

```json
{
  "profile_id": "a1b2c3d4",
  "action": "launch",
  "kind": "daily",
  "at_time": "09:00",
  "days": [0, 1, 2, 3, 4],
  "run_minutes": 15
}
```

- `kind` is `"interval"` with `interval_minutes`, or `"daily"` with `at_time`
  (`HH:MM`, 24-hour, on the **server's** clock) and optional `days` (`0` =
  Monday … `6` = Sunday; omitted = every day). A body whose fields do not match
  its `kind` fails validation.
- Creating against an unknown profile returns `400`. Deleting a profile deletes
  its schedules.
- The response carries `profile_name`, the planned `next_run_at` and the
  `last_run`.

`PUT` updates a schedule; omitted fields are left alone, and changing the timing
(or re-enabling) recomputes `next_run_at` from now. `enabled: false` pauses a
schedule.

`POST .../run` runs the action now, records the outcome, and does not move the
planned next run; it works on a paused schedule too. It returns the run record:

```json
{"schedule_id": "x9y8z7w6", "outcome": "ok", "message": "Browser launched", ...}
```

`GET .../runs` lists the newest runs first; the last 20 are kept per schedule.
`outcome` is `ok`, `skipped` (the browser was already running), `error` (the
message says why), or `missed` (it fell due while the app was not running, and
was not replayed).

## Groups

```http
GET    /api/v1/groups
POST   /api/v1/groups            {"name": "Client A", "description": "..."}
GET    /api/v1/groups/{id}
PUT    /api/v1/groups/{id}
DELETE /api/v1/groups/{id}
```

Deleting a group keeps its profiles; they become ungrouped.

## Bulk editing

```http
GET  /api/v1/profiles/export/excel     → .xlsx
POST /api/v1/profiles/import/excel     multipart: file=<sheet.xlsx>
```

Import always creates new profiles. The export contains proxy passwords in clear
text. See [excel.md](excel.md).

## System

```http
GET  /health                                liveness and database state
GET  /api/v1/system/status                  counts, load, memory, disk, uptime
GET  /api/v1/system/info                    name, version, uptime
GET  /api/v1/system/config                  effective configuration, no secret values
GET  /api/v1/system/profiles/diagnostic     profiles whose storage looks wrong
POST /api/v1/system/profiles/cleanup        remove orphaned profile directories (?dry_run=true to preview)
POST /api/v1/system/restart                 close all browsers, ready for a restart
```

`/health` is unversioned, so probes survive API versions. An unhealthy instance
answers with the same body under a `503`.

`/api/v1/system/config` reports the version, bind address, database path,
whether encryption, the API key and user login are on, the CORS origins, and
whether the browser is installed and which release. It never returns secret
values. It backs the Settings screen.

`/api/v1/system/restart` does **not** restart the process; it closes every
browser so you can restart it yourself.

## Example: create a profile and run it

```bash
API=http://localhost:8000

ID=$(curl -s -X POST $API/api/v1/profiles \
      -H 'Content-Type: application/json' \
      -d '{"name":"demo","browser_settings":{"os":"linux"}}' \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

curl -s -X POST $API/api/v1/profiles/$ID/launch \
     -H 'Content-Type: application/json' -d '{"mode":"headless"}'
# -> {"status": "launched", "cdp_endpoint": "ws://localhost:8000/api/v1/profiles/<id>/cdp", ...}

curl -s $API/api/v1/browsers/active
curl -s -X POST $API/api/v1/profiles/$ID/close
```
