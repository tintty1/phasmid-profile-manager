# API cheat sheet

Base: `$CPM_URL/api/v1` (default `http://127.0.0.1:8000/api/v1`). JSON in and out.
Auth: `X-API-Key: $CPM_API_KEY` when the server sets a key. A running server also
serves its full generated schema at `/docs` — check there when a field here
seems missing.

## Contents
- Profiles: list, get, create, update, delete, clone, fingerprint
- Running browsers: launch, modes, Playwright, close
- Proxies
- Groups, Trash, presets, browser install
- Response shapes

## Profiles

```http
GET    /profiles?page=1&per_page=25&search=shop&status=active&group=<id>
GET    /profiles/{id}
POST   /profiles                      create — only "name" is required
PUT    /profiles/{id}                 update — same shape as create, merged
DELETE /profiles/{id}                 move to Trash (409 if running)
POST   /profiles/{id}/clone           {"new_name": "...", "regenerate_fingerprint": true}
POST   /profiles/{id}/reset-fingerprint     new settings and new hardware on next launch
POST   /profiles/{id}/refresh-browser       move the pin onto the installed browser version
GET    /profiles/{id}/stats
GET    /profiles/{id}/export          zip with cookies and proxy password — treat as the account
POST   /profiles/import               multipart file=<archive.zip>
```

List returns `{profiles, total, page, per_page, has_next, has_prev}`; `search`
matches the name. Profile ids are short strings like `a1b2c3d4`.

Create / update body (every field optional except `name` on create):

```json
{
  "name": "shop-de-01",
  "group": "<group id>",
  "notes": "free text",
  "fingerprint_preset": "windows:42",
  "protected": false,
  "browser_settings": {
    "os": "windows",
    "languages": ["de-DE", "de"],
    "timezone": "Europe/Berlin",
    "geolocation": {"lat": 52.52, "lon": 13.405},
    "hardware_concurrency": 8,
    "window_width": 1280,
    "window_height": 720,
    "webrtc_mode": "replace"
  },
  "proxy_config": {"type": "socks5", "server": "host:1080", "username": "u", "password": "p"},
  "row_version": 7
}
```

- `os`: `windows`, `macos` or `linux`. Unset `browser_settings` fields are
  generated as one consistent fingerprint; prefer leaving them unset.
- `status` (update only): `active`, `inactive`, `blocked`, `maintenance` — bookkeeping.
- `protected`: while true, `DELETE`, the Trash purge, `reset-fingerprint` and
  `reconcile-os` with `keep_machine: false` answer `409` with code `protected`.
  Clones start unprotected. Only the user decides to set it back to false.
- Update: sent fields win (including `null`, which clears — `{"proxy_config": null}`
  detaches the proxy); omitted fields are left alone; `browser_settings` merges.
- `row_version` (from the last read) guards against overwriting someone else's
  save: `409 stale_write` → re-read and reapply.
- `fingerprint_preset` ids come from `GET /fingerprints/presets?os=windows`
  (`data.presets[].id`) — a real captured device.

## Running browsers

```http
GET  /browsers/modes                  {"default": "virtual", "modes": [{"mode", "available", "reason"}]}
POST /profiles/{id}/launch            {"mode": "headless" | "virtual" | "window"}
GET  /browsers/active                 {"active_browsers": [{"profile_id", "mode", "playwright", "vnc", ...}], "count"}
POST /profiles/{id}/close
POST /browsers/close-all              stops every browser — only when asked
WS   /profiles/{id}/playwright        Playwright's protocol; connect with firefox.connect()
```

Launch response:

```json
{
  "profile_id": "a1b2c3d4",
  "status": "launched",
  "mode": "headless",
  "playwright_endpoint": "ws://127.0.0.1:8000/api/v1/profiles/a1b2c3d4/playwright",
  "process_id": 1234,
  "message": "Browser launched successfully"
}
```

- `status` is `already_running` when it was up already; the endpoint is the same.
- Optional launch fields: `window_size` (`"1280x720"`), `additional_options`
  (raw Camoufox options — avoid unless you know why).
- `409` on launch: another manager instance holds the profile.
- `500` with a message about the display: that mode is unavailable here — check
  `/browsers/modes` and pick another.
- `playwright_endpoint` is `null` only if the browser could not be served to
  Playwright; close and relaunch.

## Proxies

```http
POST /profiles/{id}/check-proxy       checks the saved proxy, stores the answer on the profile
POST /proxy/check                     {"proxy_config": {...}} — an unsaved proxy
```

Answer: `reachable`, `error`, `latency_ms`, `location` (`ip`, `country`,
`timezone`, …) and `findings` (`level`: error/warning/info). An unreachable proxy
is `reachable: false`, not an HTTP error. Proxy `type`: `http`, `https`,
`socks4`, `socks5`; `server` is `host:port`.

## Groups, Trash, presets, setup

```http
GET/POST       /groups                {"name": "Client A", "description": "..."}
GET/PUT/DELETE /groups/{id}           deleting a group ungroups its profiles
GET    /trash/profiles
POST   /trash/profiles/{id}/restore
DELETE /trash/profiles/{id}?confirm=true      permanent — only when explicitly asked
GET    /fingerprints/presets?os=windows
GET    /system/browser                {installed, version, state: idle|downloading|ready|error, progress}
POST   /system/browser/install        starts the download; poll GET until ready
```

Envelope endpoints (Trash, presets, system) wrap results as
`{"success", "message", "data"}`; resource endpoints return the resource.

## Errors

```json
{"error": {"code": "not_found", "message": "Profile with ID x1 not found", "details": null}}
```

`bad_request` 400 · `unauthorized` 401 · `not_found` 404 · `conflict` /
`stale_write` / `protected` 409 · `validation_error` 422 (`details` = pydantic field errors) ·
`internal_error` 500.
