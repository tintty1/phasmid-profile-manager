---
name: phasmid-profile-manager
description: Drive Phasmid Profile Manager (phasmid-pm) to automate websites in persistent anti-detect browser profiles — create, find, edit, clone and delete profiles (machine, OS, GPU, proxy), launch a profile headless, on a virtual display or as a desktop window, and control it over CDP (Playwright connect_over_cdp, Puppeteer) through its cdp_endpoint. Use this whenever a task involves Phasmid, phasmid-pm, a "browser profile", an anti-detect or fingerprinted browser, a profile manager running on localhost:8000, or automating a site as a specific saved account/identity — even if the user only says "log in with profile X", "open shop-de-01 and check the orders page", or "spin up a few browser profiles behind these proxies".
---

# Phasmid Profile Manager

Phasmid Profile Manager keeps **profiles**: each one is a persistent Chromium
(Phasmid) identity — its own pinned machine (a Phasmid profile: OS, GPU, screen,
fonts, CPU, memory), proxy, cookies and storage. It runs as a server (by default `http://127.0.0.1:8000`, often via
`docker compose up`) with a REST API under `/api/v1` and a web UI at `/`.

You manage profiles over REST, launch a profile's browser, then drive that browser
over **CDP** through a WebSocket the launch hands back. The browser keeps the
profile's cookies and logins between sessions, which is the whole point: you are
acting *as that account*, on the same "machine" every time.

## Setup

- `PPM_URL` — the server (default `http://127.0.0.1:8000`). Check it is up with
  `curl -s $PPM_URL/health`.
- `PPM_API_KEY` — only if the server sets one; send it as `X-API-Key` on REST
  calls and as an `x-api-key` header when connecting over CDP. A `401` means it
  is missing or wrong.
- Any recent Playwright (`pip install playwright`) or Puppeteer works: CDP is
  Chrome's own protocol, so there is no client/server version lock. You do
  **not** need `playwright install`: the browser runs on the server, you only
  connect to it.

## The helper script

`scripts/ppm.py` (next to this file) wraps the API so you don't rebuild requests
by hand. The shell side uses only the standard library and prints JSON; profile
arguments accept an id or an exact name.

```bash
python scripts/ppm.py modes                       # what this server can launch in
python scripts/ppm.py list --search shop
python scripts/ppm.py gpus --os windows             # GPUs a new machine may claim
python scripts/ppm.py create shop-de-01 --os windows --proxy socks5://user:pass@host:1080
python scripts/ppm.py machine shop-de-01           # the whole pinned Phasmid profile
python scripts/ppm.py update shop-de-01 --json '{"notes": "2FA on"}'
python scripts/ppm.py launch shop-de-01 --mode headless
python scripts/ppm.py close shop-de-01
python scripts/ppm.py protect shop-de-01         # refuse deletes / new hardware from now on
python scripts/ppm.py delete shop-de-01          # to Trash; stop it first
```

From Python, `open_profile` launches (or finds running) and connects in one step:

```python
import sys; sys.path.insert(0, "<this skill>/scripts")
from ppm import open_profile

async with open_profile("shop-de-01", mode="headless") as (page, context, info):
    await page.goto("https://example.com/account")
    ...
```

Use the script, or call the API directly when you need something it doesn't
cover — [references/api.md](references/api.md) has every endpoint and body.

## Workflow

1. **Find or create the profile.** Look it up first (`list --search`, `get`);
   create one only when the task needs a new identity. Reusing the right profile
   matters more than anything else here — a fresh profile is a new, logged-out
   stranger to every site.
2. **Pick a mode** (`modes` shows what this server offers and its default):
   - `headless` — no display at all. The default choice for unattended work.
   - `virtual` — the browser draws on its own VNC display, which a person can
     watch and take over from the web UI (Run → monitor button). Choose this
     when a human might need to step in: a CAPTCHA, a 2FA prompt, a login you
     can't complete, or when the user wants to watch.
   - `window` — a real window on the server machine's desktop. Only when the user
     asks for it; it is often unavailable in Docker.
   Leaving `mode` out uses the server's default.
3. **Launch, then connect.** `POST /profiles/{id}/launch` returns
   `cdp_endpoint`; connect with `chromium.connect_over_cdp(endpoint)` (or
   `puppeteer.connect({browserWSEndpoint: endpoint})`). Launch is
   idempotent: a profile that is already running answers `already_running` with
   the same endpoint and keeps its current mode, so always launch-then-connect
   rather than guessing whether it is up.
4. **Work in the profile's own context.** Use `browser.contexts[0]` and its
   existing page (`context.pages[0]`) or `context.new_page()`. That context *is*
   the profile — its cookies, storage and machine.
5. **Finish.** `browser.close()` on your side only **disconnects** you; the
   profile keeps running. If you launched it for this task and nobody needs it
   afterwards, stop it with `POST /profiles/{id}/close` (or
   `open_profile(..., stop_on_exit=True)`). Leave alone browsers you didn't start.

```python
from playwright.async_api import async_playwright

launch = POST(f"/profiles/{pid}/launch", {"mode": "headless"})
async with async_playwright() as p:
    browser = await p.chromium.connect_over_cdp(launch["cdp_endpoint"],
                                                headers={"x-api-key": key} if key else None)
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://example.com")
    await browser.close()            # disconnect; the profile stays up
POST(f"/profiles/{pid}/close")      # stop it, if you started it
```

## Things that will bite you

- **Don't `browser.new_context()`.** A new context is a blank, non-persistent
  browser with none of the profile's cookies — anything you log into there is
  lost, and it isn't the identity you were asked to use.
- **Logins persist; "session-only" cookies don't.** Stopping and relaunching a
  profile keeps everything a normal browser keeps: cookies with an expiry date
  (most logins, Google's included), localStorage, IndexedDB, saved passwords.
  Like any Chrome, it drops cookies the site set *without* an expiry when the
  browser exits. So when you log in for later use, tick "stay signed in" /
  "remember me" if the site offers it; on a site that still keeps you logged in
  only for the session, leave the profile running instead of stopping it.
- **Don't fight the machine.** Avoid `set_viewport_size`, `Emulation.*`
  overrides, and overriding the user agent, timezone, locale or geolocation from
  the client. Phasmid reports one consistent machine from inside the browser, in
  every frame and worker; an override from outside reaches only some of them,
  which is exactly what detection looks for. Change settings on the profile
  instead (`PUT`) and relaunch.
- **One browser per profile.** Two agents can connect to the same running profile
  and will share its pages — coordinate, or use separate profiles for parallel
  work. A `409 conflict` on launch means another manager instance has it open.
- **Edits are merged and versioned.** `PUT /profiles/{id}` merges
  `browser_settings` over what is stored; a field set to `null` is cleared, an
  omitted field is left alone. Send the `row_version` you read; `409
  stale_write` means someone saved in between — re-read and reapply. Settings
  apply on the next launch, so close and relaunch a running profile after editing.
- **Protected profiles are off limits.** A profile with `"protected": true` is
  one the user cares about (a warmed-up account). Moving it to Trash, purging
  it, `reset-fingerprint` and `reconcile-os` with `keep_machine: false` are
  refused with `409` and error code `protected`. Treat that as a final answer:
  report it to the user. Never set `protected` to `false` yourself to get
  past it — lifting protection is the user's decision, so only do it when they
  explicitly ask for that profile. Edits, launching and cloning still work.
- **Deleting needs a stopped profile** (`409` otherwise) and only moves it to
  Trash; erasing it for good is a separate, deliberate call. Don't erase
  profiles unless the user clearly asked — they hold real accounts' sessions.
  When you create a profile the user will keep (a real account), offer to
  create it with `--protected`.
- **Clones get new hardware** by default (`regenerate_fingerprint: true`) — that
  is right for a second account. Copying the machine makes two "different"
  accounts provably one computer.
- **Geography follows the proxy.** Leave `timezone` and `languages` unset and
  every launch places the machine at its exit address: timezone, position,
  languages and the WebRTC address, with the hardware untouched. A launch whose
  proxy does not answer is refused. Run `POST /profiles/{id}/check-proxy`
  before relying on a new proxy.
- **OS and GPU are the hardware.** Changing them on a profile that already has
  a machine does nothing until `reset-fingerprint` gives it a new one, which a
  warmed-up account should not get lightly.
- **Setup first.** Launches fail with a "browser needs setup" error until
  Phasmid is installed: start it with `POST /api/v1/system/browser/install` and
  poll `GET /api/v1/system/browser` until `state` is `ready`.

## Errors

Every error body is `{"error": {"code", "message", "details"}}` — read
`error.message`; it usually says what to do. Codes: `not_found` (404),
`conflict` / `stale_write` / `protected` (409), `validation_error` (422,
`details` lists the fields), `unauthorized` (401). The CDP relay closes the socket with 4401
(auth), 4404 (profile not running — launch it first) or 4403 (origin).
