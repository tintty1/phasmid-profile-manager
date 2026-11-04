# API cheat sheet

Base: `$PPM_URL/api/v1` (default `http://127.0.0.1:8000/api/v1`). JSON in and out.
Auth: `X-API-Key: $PPM_API_KEY` when the server sets a key. A running server also
serves its full generated schema at `/docs`; check there when a field here
seems missing.

## Contents
- Profiles: list, get, create, update, delete, clone, machine
- Running browsers: launch, modes, CDP, close
- Proxies
- Groups, Trash, GPUs, browser install
- Errors

## Profiles

```http
GET    /profiles?page=1&per_page=25&search=shop&status=active&group=<id>
GET    /profiles/{id}
GET    /profiles/{id}/machine         the whole pinned Phasmid profile JSON (404 until generated)
POST   /profiles                      create; only "name" is required
PUT    /profiles/{id}                 update; browser_settings is merged
DELETE /profiles/{id}                 move to Trash (409 if running)
POST   /profiles/{id}/clone           {"new_name": "...", "regenerate_fingerprint": true, "copy_data": true}
GET    /profiles/{id}/downloads[?guid=]   {"downloads_dir", "files": [{"name", "size", "modified", "complete", "guid"}]}
GET    /profiles/{id}/downloads/{name}      the file's bytes
DELETE /profiles/{id}/downloads/{name}
POST   /profiles/{id}/reset-fingerprint     new seed, new hardware
POST   /profiles/{id}/reconcile-os          {"keep_machine": true}
GET    /profiles/{id}/stats
GET    /profiles/{id}/export          zip with cookies and proxy password; treat as the account
POST   /profiles/import               multipart file=<archive.zip>
```

List returns `{profiles, total, page, per_page, has_next, has_prev}`; `per_page`
is at most 100 and `search` matches the name. Profile ids are short strings like
`a1b2c3d4`.

Create / update body (every field optional except `name` on create):

```json
{
  "name": "shop-de-01",
  "group": "<group id>",
  "notes": "free text",
  "protected": false,
  "browser_settings": {
    "os": "windows",
    "gpu": "0x10de:0x2504",
    "languages": null,
    "timezone": null,
    "webrtc_policy": "default"
  },
  "proxy_config": {"type": "socks5", "server": "host:1080", "username": "u", "password": "p"},
  "row_version": 7
}
```

- `os`: `linux` (default) or `windows`. `gpu`: a `gpu` value from
  `GET /fingerprints/gpus`; unset, one is drawn from captured devices. Both
  pick the hardware and only take effect when a machine is generated (on
  create, first launch, clone or `reset-fingerprint`).
- `languages`, `timezone`: leave unset. Unset, each launch derives them, the
  position and the WebRTC address from the proxy's exit. A stated timezone wins
  over the exit and drops the position.
- `webrtc_policy`: `default`, `default_public_interface_only`,
  `default_public_and_private_interfaces`, `disable_non_proxied_udp`.
- `status` (update only): `active`, `inactive`, `blocked`, `maintenance`; bookkeeping.
- `protected`: while true, `DELETE`, the Trash purge, `reset-fingerprint` and
  `reconcile-os` with `keep_machine: false` answer `409` with code `protected`.
  Clones start unprotected. Only the user decides to set it back to false.
- Update: sent fields win (including `null`, which clears: `{"proxy_config": null}`
  detaches the proxy); omitted fields are left alone; `browser_settings` merges.
- `row_version` (from the last read) guards against overwriting someone else's
  save: `409 stale_write` means re-read and reapply.
- The profile's `fingerprint` is a summary of the machine (OS, platform, cores,
  memory, screen, GPU, timezone, languages, `public_ip`, `os_mismatch`, ...), or
  `null` before one is generated.

## Running browsers

```http
GET  /browsers/modes                  {"default": "virtual", "modes": [{"mode", "available", "reason"}]}
POST /profiles/{id}/launch            {"mode": "headless" | "virtual" | "window", "args": ["--switch"]}
GET  /browsers/active                 {"active_browsers": [{"profile_id", "mode", "cdp", "vnc", ...}], "count"}
POST /profiles/{id}/close
POST /browsers/close-all              stops every browser; only when asked
WS   /profiles/{id}/cdp               CDP; chromium.connect_over_cdp(cdp_endpoint)
```

Launch response:

```json
{
  "profile_id": "a1b2c3d4",
  "status": "launched",
  "mode": "headless",
  "cdp_endpoint": "ws://127.0.0.1:8000/api/v1/profiles/a1b2c3d4/cdp",
  "process_id": 1234,
  "message": "Browser launched successfully",
  "downloads_dir": "/data/downloads/a1b2c3d4",
  "launch": {"exit_ip": "203.0.113.7", "moved": false, "argv": ["..."], "process_id": 1234}
}
```

- `downloads_dir` is on the server. Downloads a client starts through the relay
  land there under their own names (`name (1).ext` for repeats); find one with
  `?guid=` (Playwright: the file name of `download.path()`) and fetch it from
  `/profiles/{id}/downloads/<name>`. `download.save_as()` is not reliable
  remotely.

- `status` is `already_running` when it was up already; the endpoint is the same.
- Connect with `chromium.connect_over_cdp(cdp_endpoint, headers={"x-api-key": key})`
  or `puppeteer.connect({browserWSEndpoint: cdp_endpoint})`. Any recent client
  version works. Use `browser.contexts[0]`; `browser.close()` only disconnects.
- `args` (optional): extra Chrome switches. Avoid anything a page could observe.
- `409` on launch: another manager instance holds the profile.
- `400`: genprofile could not generate or move the machine (for example an exit
  address it cannot place).
- `500` with a message: the browser is not installed, the proxy did not answer
  (the launch is refused rather than started at the wrong location), the mode is
  unavailable here (check `/browsers/modes`), or the browser failed to start.
- The relay closes with `4401` (auth), `4403` (origin), `4404` (profile not
  running; launch it first) or `1011` (browser unavailable).

## Proxies

```http
POST /profiles/{id}/check-proxy       checks the saved proxy, stores the answer on the profile
POST /proxy/check                     {"proxy_config": {...}, "browser_settings": {...}}; an unsaved proxy
```

Answer: `reachable`, `error`, `latency_ms`, `location` (`ip`, `country`,
`timezone`, `latitude`, `longitude`) and `findings` (`level`: error/warning/info).
An unreachable proxy is `reachable: false`, not an HTTP error. Proxy `type`:
`http`, `https`, `socks5` (`socks4` is accepted but refused at launch);
`server` is `host:port`.

## Groups, Trash, GPUs, setup

```http
GET/POST       /groups                {"name": "Client A", "description": "..."}
GET/PUT/DELETE /groups/{id}           deleting a group ungroups its profiles
GET    /trash/profiles
POST   /trash/profiles/{id}/restore
DELETE /trash/profiles/{id}?confirm=true      permanent; only when explicitly asked
GET    /fingerprints/gpus?os=windows  data.gpus: [{os, gpu, renderer, weight}]
GET    /system/browser                {installed, version, state: idle|downloading|ready|error, progress}
POST   /system/browser/install        starts the download; poll GET until ready
```

Envelope endpoints (Trash, GPUs, system) wrap results as
`{"success", "message", "data"}`; resource endpoints return the resource.

## Errors

```json
{"error": {"code": "not_found", "message": "Profile with ID x1 not found", "details": null}}
```

`bad_request` 400 · `unauthorized` 401 · `not_found` 404 · `conflict` /
`stale_write` / `protected` 409 · `validation_error` 422 (`details` = pydantic field errors) ·
`internal_error` 500.
