# Security Policy

## Supported versions

This project is pre-1.0 and under active development. Security fixes are applied
to the latest release only.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

Please report security issues privately using GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
("Report a vulnerability" under the repository's **Security** tab), rather than
opening a public issue.

Please include a description of the issue, the affected component, and steps to
reproduce. We aim to acknowledge reports within a few days.

## Handling secrets

- **Never commit real proxy credentials, API keys, or profile data.** The
  repository ships only synthetic examples. `data/`, `*.db`, `.env`, and profile
  directories are git-ignored.
- Proxy passwords are encrypted at rest when `PPM_SECRET_KEY` is set (see
  `.env.example`). Generate a key with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- If a credential is ever committed by mistake, **rotate it immediately** — removing
  it from the latest commit does not remove it from git history.

## Authentication

The API has three configuration states, decided by what exists rather than by a
switch, so the configuration cannot contradict the database:

1. **Nothing configured** — no user accounts, no `PPM_API_KEY`. The API is
   open. This is the default and is acceptable only because the app binds to
   `127.0.0.1`; it is how most single-person installs run.
2. **`PPM_API_KEY` set** — every request must carry the key in `X-API-Key`,
   compared in constant time. This is the machine-to-machine path: one shared
   secret, no identity.
3. **User accounts exist** (`phasmid-pm user add <name>`) — humans log in with
   a username and password; a valid session **or** the API key passes. Accounts
   are created, changed and removed only from the CLI — there is no
   self-registration, so creating an account requires shell access to the host.
   Removing the last account turns login back off.

What protects a session:

- Passwords are hashed with **argon2id** (`argon2-cffi` defaults); plaintext
  passwords are never stored or logged, and hashes never leave the database.
- The session token is random (256 bits) and stored **only as its SHA-256**, so
  a copy of the database contains nothing replayable.
- The cookie is **HttpOnly** (page scripts cannot read it), **SameSite=Lax**
  (cross-site POSTs do not carry it), and **Secure** when served over HTTPS or
  when `PPM_SECURE_COOKIES=1`.
- Logout deletes the server-side row: a logged-out token is invalid even if an
  attacker kept a copy. Sessions expire after `PPM_SESSION_TTL_HOURS`
  (default 168).
- A failed login does not reveal whether the username exists — same status,
  same body, the same argon2 work either way — and is delayed half a second as
  brute-force friction. There is **no lockout or rate limit** beyond that; if
  the login page is reachable from the internet, put fail2ban or your reverse
  proxy's rate limiting in front of it.

## Deployment notes

- The API binds to `127.0.0.1` by default. To expose it beyond localhost:
  create a user account (`phasmid-pm user add <name>`) for humans and/or set
  `PPM_API_KEY` for machine clients, terminate TLS in a reverse proxy, and set
  `PPM_SECURE_COOKIES=1` so the session cookie is never sent over plain HTTP
  (the app itself does not serve TLS, so it cannot infer this behind a proxy).
- Set `PPM_CORS_ORIGINS` to the specific origins you trust; do not use a wildcard
  together with credentials.
- The proxy check connects to whatever host a request names and reports whether it
  answered and how fast. On an instance reachable beyond localhost without any
  authentication configured, that is an unauthenticated way to probe hosts and
  ports the server can reach but the caller cannot.

## Running browsers

- **The CDP relay is full control.** A client connected to
  `/api/v1/profiles/{id}/cdp` can do anything the browser can: read every
  cookie and saved login, and act as the account. It is guarded by the same
  authentication as the API, plus an Origin check against cross-site pages.
- **Chrome's own CDP port is unauthenticated.** Each browser listens on a
  loopback port it picks (`--remote-debugging-port=0`, written to
  `DevToolsActivePort` in the profile directory). Any process on this host that
  finds the port can drive the browser without going through the relay. Do not
  run the manager on a host shared with users you do not trust.
- **proxyroute's relay listener is too.** For a proxy with credentials,
  proxyroute opens a SOCKS5 listener on loopback without authentication, so any
  local user can use the proxy, with its credentials, while the browser runs
  (see Phasmid's `tools/proxyroute/README.md`).
- Proxy credentials are never put on a command line: they are written to
  `<data dir>/run/<id>/proxy-args` (mode 0600, in a 0700 directory) and passed
  as `@file`. The run directory is removed when the session ends.
- The VNC displays listen on loopback with no password; the web UI reaches them
  only through the authenticated `/api/v1/profiles/{id}/vnc` relay.

## What the app downloads and contacts

- **Install browser** downloads a Phasmid release archive from GitHub and checks
  it against the `.sha256` published in the same release. That detects a
  corrupted download, not a compromised release. The source tree at the same
  tag is downloaded from GitHub over TLS with no further check, and its
  `tools/proxyroute` runs with the app's own Python interpreter. Archives are
  unpacked with tarfile's `data` filter, which refuses absolute paths, `..`,
  device files and links leaving the archive. phasmid-profile-gen, a pinned
  PyPI dependency, downloads its DB-IP database and checks it against the
  SHA-256 pinned in the package.
- **Every launch and every proxy check** asks a plain-text IP echo service,
  through the profile's proxy, for the exit address: `api.ipify.org`, then
  `checkip.amazonaws.com`, then `icanhazip.com`, over verified TLS. Without a
  proxy that reveals this computer's public address to those services.
- **The update check** asks the GitHub API for this project's latest release.

