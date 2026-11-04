---
name: phasmid-profile-manager
description: Create, copy (clone) and destroy persistent anti-detect browser profiles in Phasmid Profile Manager (phasmid-pm), and browse the web as one of them with Playwright over its CDP URL (chromium.connect_over_cdp). Use this whenever a task involves Phasmid, phasmid-pm, a "browser profile", an anti-detect or fingerprinted browser, a profile manager on localhost:8000, or doing something on a website as a specific saved account/identity — even if the user only says "log in with profile X", "make three copies of shop-de-01", "delete the test profiles", or "open this page from the Tokyo profile".
---

# Phasmid Profile Manager

A **profile** is one persistent identity: a pinned machine (a Phasmid profile —
OS, GPU, screen, fonts, CPU, memory, generated once from real-device data), a
proxy, and its own Chromium user data (cookies, logins, storage, history). The
manager runs as a server (default `http://127.0.0.1:8000`, often `docker
compose up`) with a REST API under `/api/v1` and a web UI at `/`.

You manage profiles over REST and browse by launching a profile's browser and
attaching **Playwright over CDP** to the URL the launch returns. Every launch of
a profile is the same computer, with the same cookies, so a site sees one
returning user, not a new stranger.

## Setup

- `PPM_URL` — the server (default `http://127.0.0.1:8000`). Check it is up:
  `curl -s $PPM_URL/health`.
- `PPM_API_KEY` — only if the server sets one; sent as `X-API-Key` on REST calls
  and as an `x-api-key` header on the CDP connection. `401` means missing/wrong.
- `pip install playwright` (any recent version; CDP has no version lock). You do
  **not** need `playwright install` — the browser runs on the server.
- A launch fails with "browser needs setup" until Phasmid is installed:
  `POST /api/v1/system/browser/install`, then poll `GET /api/v1/system/browser`
  until `state` is `ready`.

Two helpers sit next to this file. `scripts/ppm.py` (standard library only)
does profile management and prints JSON; profile arguments take an id or an
exact name. `scripts/browse.py` opens a URL in a profile and reports what it
shows. Run them from anywhere; for anything they don't cover,
[references/api.md](references/api.md) lists every endpoint.

## Create a profile

Look first — reusing the right profile matters more than anything else, since a
new profile is a logged-out stranger to every site:

```bash
python scripts/ppm.py list --search shop
python scripts/ppm.py get shop-de-01
```

Create one only when the task needs a new identity:

```bash
python scripts/ppm.py create shop-de-01 --os windows \
    --proxy http://user:pass@proxy.example:8080
python scripts/ppm.py create research-01                 # Linux, direct connection
python scripts/ppm.py gpus --os linux                    # GPUs a machine may claim
python scripts/ppm.py create gpu-pick --os linux --gpu 0x10de:0x2504 --protected
```

- `--os` is `linux` (default) or `windows`; `--gpu` takes a `vendor:device` from
  `gpus`. Both choose the hardware once, at creation.
- Proxies: `http`, `https`, `socks5` (all with credentials) or `socks4` (none).
  Check one before relying on it: `POST /api/v1/profiles/{id}/check-proxy`.
- **Leave timezone and languages unset.** Every launch looks up where the proxy
  exits and places the machine there (timezone, position, languages, WebRTC
  address); the hardware never changes. Set them only when the user asks.
- Offer `--protected` for a profile the user will keep (a real account): it
  refuses deletion and new hardware until the user lifts it.

## Copy a profile

```bash
python scripts/ppm.py clone shop-de-01 shop-de-02
```

A clone copies the settings, the proxy and the browser data (cookies, logins,
storage; caches are left behind), and gets **new hardware** — that is right for
a second account. `--same-machine` keeps the hardware too, which makes the two
profiles provably one computer to any site that sees both; use it only when the
user means "move/duplicate this exact machine". Both copies share the source's
proxy, so give the clone its own before running both:

```bash
python scripts/ppm.py update shop-de-02 --json \
  '{"proxy_config": {"type": "http", "server": "proxy2.example:8080", "username": "u", "password": "p"}}'
```

A copy of a logged-in profile is logged in too — the same session, now in two
places. For a fresh account, create rather than clone.

## Destroy a profile

Two steps, on purpose:

```bash
python scripts/ppm.py delete shop-de-02        # to Trash: recoverable
python scripts/ppm.py trash                    # what is in Trash
python scripts/ppm.py restore shop-de-02       # undo
python scripts/ppm.py purge shop-de-02 --yes   # erase from Trash, for good
python scripts/ppm.py destroy shop-de-02 --yes # close + Trash + erase in one go
```

- `delete` needs the browser closed (`409` while it runs); `destroy` closes it
  first.
- Purging deletes the profile's directory — its cookies, logins and history.
  It cannot be undone. Only purge or destroy when the user clearly asked to
  delete for good; otherwise stop at `delete` and say it is in Trash.
- A **protected** profile refuses delete, purge and destroy with `409` and code
  `protected`. That is a final answer: report it. Never unprotect a profile
  yourself to get past it.

## Browse the web as a profile

One command, when you only need to look:

```bash
python scripts/browse.py shop-de-01 https://example.com/account --text
python scripts/browse.py shop-de-01 https://example.com --screenshot page.png --mode virtual
```

It runs headless unless `--mode` says otherwise, prints JSON with `status`,
`url`, `title` (and `text` / `html` / `screenshot` when asked), and leaves the
browser running unless `--stop`. Always pass a mode when launching yourself too:
left out, the server's default may open a window on someone's desktop.

To interact (log in, click, fill forms), drive it yourself:

1. **Launch.** `POST /api/v1/profiles/{id}/launch` with `{"mode": ...}` returns
   `cdp_endpoint`, e.g. `ws://127.0.0.1:8000/api/v1/profiles/<id>/cdp`. Launch
   is idempotent: a running profile answers `already_running` with the same
   endpoint, so always launch, then connect.
2. **Connect** with `chromium.connect_over_cdp(cdp_endpoint)`.
3. **Use the profile's own context**: `browser.contexts[0]`, its first page or a
   new page in it. That context *is* the profile.
4. **Disconnect** with `browser.close()` — over CDP it only drops your
   connection; the browser keeps running. Stop it with
   `POST /api/v1/profiles/{id}/close` if you started it and nobody needs it.

```python
import asyncio, os, urllib.request, json
from playwright.async_api import async_playwright

API = os.environ.get("PPM_URL", "http://127.0.0.1:8000") + "/api/v1"
KEY = os.environ.get("PPM_API_KEY")

def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"X-API-Key": KEY} if KEY else {})})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read() or b"null")

async def main(profile_id):
    launch = call("POST", f"/profiles/{profile_id}/launch", {"mode": "headless"})
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(
            launch["cdp_endpoint"], headers={"x-api-key": KEY} if KEY else None)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://example.com/login")
        await page.fill("#email", "me@example.com")
        await page.click("button[type=submit]")
        await page.wait_for_load_state("networkidle")
        print(await page.title())
        await browser.close()                       # disconnect only
    call("POST", f"/profiles/{profile_id}/close")   # stop, if you started it

asyncio.run(main("shop-de-01-id"))
```

Or from Python with the helper, which does launch/connect/disconnect for you:

```python
import sys; sys.path.insert(0, "<this skill>/scripts")
from ppm import open_profile

async with open_profile("shop-de-01", mode="headless", stop_on_exit=True) as (page, context, info):
    await page.goto("https://example.com/account")
```

Modes (`python scripts/ppm.py modes` shows what this server offers):
`headless` for unattended work; `virtual` — its own VNC display a person can
watch and take over in the web UI (pick it when a CAPTCHA, 2FA or a login only
the user can finish may come up); `window` — a real window on the server's
desktop, only when asked. An already-running profile keeps its mode.

## Things that will bite you

- **Don't `browser.new_context()`.** It is a blank, throw-away browser with none
  of the profile's cookies, and not the identity you were asked to use.
- **Don't fight the machine.** No `set_viewport_size`, `Emulation.*`
  overrides, or user agent / timezone / locale / geolocation overrides from the
  client. Phasmid reports one consistent machine from inside the browser, in
  every frame and worker; an outside override reaches only some of them, which
  is exactly what detection looks for. Change the profile (`PUT`) and relaunch.
- **Session-only cookies go when the browser stops.** Logins with an expiry
  date persist across launches; tick "remember me" when logging in for later,
  or leave the profile running.
- **One browser per profile.** Two clients on one running profile share its
  pages. `409` on launch means another manager instance has it open.
- **A proxy that does not answer stops the launch** (the exit address could
  not be looked up). Fix or check the proxy rather than retrying.
- **Edits merge and are versioned.** `PUT /profiles/{id}` merges
  `browser_settings`; send the `row_version` you read (`ppm.py update` does).
  `409 stale_write` means someone saved in between: re-read and reapply.
  Settings apply at the next launch.
- **OS and GPU are the hardware.** Changing them on an existing profile does
  nothing until `POST /profiles/{id}/reset-fingerprint` gives it a new machine
  — a real cost for a warmed-up account; don't do it unasked.

## Errors

Every error body is `{"error": {"code", "message", "details"}}`; `message`
usually says what to do. Codes: `not_found` (404), `conflict` / `stale_write` /
`protected` (409), `validation_error` (422), `unauthorized` (401). The CDP relay
closes the socket with 4401 (auth), 4404 (profile not running — launch it first)
or 4403 (origin not allowed).
