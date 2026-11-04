#!/usr/bin/env python3
"""Camoufox Profile Manager helper: profile CRUD from the shell, Playwright from Python.

Configuration comes from the environment:
  CPM_URL      base URL of the manager (default http://127.0.0.1:8000)
  CPM_API_KEY  sent as X-API-Key when set

Shell (stdlib only, prints JSON):
  cpm.py modes                         which launch modes this server offers
  cpm.py list [--search TEXT] [--group ID] [--status active]
  cpm.py get ID_OR_NAME
  cpm.py create NAME [--os windows|macos|linux] [--proxy socks5://user:pass@host:port]
                     [--group ID] [--notes TEXT] [--protected] [--json '{...extra fields...}']
  cpm.py protect ID_OR_NAME                            (refuse delete / new machine)
  cpm.py update ID_OR_NAME --json '{"notes": "..."}'   (sends row_version for you)
  cpm.py delete ID_OR_NAME                             (moves to Trash)
  cpm.py launch ID_OR_NAME [--mode headless|virtual|window]
  cpm.py close ID_OR_NAME
  cpm.py active

Python (needs playwright==1.60.*, the server's version):
  from cpm import open_profile
  async with open_profile("shop-de-01", mode="headless") as (page, context, info):
      await page.goto("https://example.com")
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = os.environ.get("CPM_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE}/api/v1"
KEY = os.environ.get("CPM_API_KEY")


class CpmError(RuntimeError):
    """An API error, carrying the server's status and error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def request(method: str, path: str, body: Any = None, **query: Any) -> Any:
    """Call the API and return the decoded JSON body; raise CpmError on failure."""
    params = {k: v for k, v in query.items() if v is not None}
    url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if KEY:
        headers["X-API-Key"] = KEY
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        raise CpmError(
            exc.code, error.get("code", "http_error"), error.get("message", str(exc))
        ) from None
    except urllib.error.URLError as exc:
        raise CpmError(0, "unreachable", f"cannot reach {BASE}: {exc.reason}") from None
    return json.loads(raw) if raw else None


def resolve(id_or_name: str) -> dict[str, Any]:
    """A profile by id, or else by exact name (refusing an ambiguous name)."""
    with contextlib.suppress(CpmError):
        return request("GET", f"/profiles/{urllib.parse.quote(id_or_name)}")
    found = [
        p
        for p in request("GET", "/profiles", search=id_or_name, per_page=100)["profiles"]
        if p["name"] == id_or_name
    ]
    if not found:
        raise CpmError(404, "not_found", f"no profile with id or name {id_or_name!r}")
    if len(found) > 1:
        ids = ", ".join(p["id"] for p in found)
        raise CpmError(409, "ambiguous", f"{len(found)} profiles are named {id_or_name!r}: {ids}")
    return found[0]


def parse_proxy(url: str) -> dict[str, Any]:
    """socks5://user:pass@host:port -> the API's proxy_config."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https", "socks4", "socks5") or not parts.hostname:
        raise SystemExit(f"bad proxy {url!r}: expected scheme://[user:pass@]host:port")
    config: dict[str, Any] = {"type": parts.scheme, "server": f"{parts.hostname}:{parts.port}"}
    if parts.username:
        config["username"] = urllib.parse.unquote(parts.username)
        config["password"] = urllib.parse.unquote(parts.password or "")
    return config


def launch(id_or_name: str, mode: str | None = None) -> dict[str, Any]:
    """Start the profile's browser (or find it running); returns the launch response.

    Idempotent: a running profile answers status "already_running" with the same
    playwright_endpoint, so launching first and connecting second is always safe.
    """
    profile = resolve(id_or_name)
    return request("POST", f"/profiles/{profile['id']}/launch", {"mode": mode} if mode else {})


@contextlib.asynccontextmanager
async def open_profile(id_or_name: str, mode: str | None = None, stop_on_exit: bool = False):
    """Launch a profile and yield (page, context, launch_info) driven by Playwright.

    The context is the profile's own persistent one - its cookies, storage and
    fingerprint. Leaving the block disconnects; the browser keeps running unless
    stop_on_exit is True (or you call close()).
    """
    from playwright.async_api import async_playwright

    info = launch(id_or_name, mode)
    endpoint = info.get("playwright_endpoint")
    if not endpoint:
        raise CpmError(
            503, "no_playwright", f"{info['profile_id']} has no Playwright endpoint: {info}"
        )
    headers = {"x-api-key": KEY} if KEY else None
    try:
        async with async_playwright() as p:
            browser = await p.firefox.connect(endpoint, headers=headers)
            try:
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                yield page, context, info
            finally:
                await browser.close()  # disconnects this client only
    finally:
        if stop_on_exit:
            with contextlib.suppress(CpmError):
                request("POST", f"/profiles/{info['profile_id']}/close")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("modes")
    sub.add_parser("active")
    p = sub.add_parser("list")
    p.add_argument("--search")
    p.add_argument("--group")
    p.add_argument("--status")
    for name in ("get", "delete", "close", "protect"):
        sub.add_parser(name).add_argument("profile")
    p = sub.add_parser("create")
    p.add_argument("name")
    p.add_argument("--os", choices=["windows", "macos", "linux"])
    p.add_argument("--proxy")
    p.add_argument("--group")
    p.add_argument("--notes")
    p.add_argument("--protected", action="store_true", help="refuse deleting it later")
    p.add_argument("--json", default="{}", help="extra create fields as JSON")
    p = sub.add_parser("update")
    p.add_argument("profile")
    p.add_argument("--json", required=True, help="fields to change as JSON")
    p = sub.add_parser("launch")
    p.add_argument("profile")
    p.add_argument("--mode", choices=["headless", "virtual", "window"])
    args = parser.parse_args()

    try:
        if args.cmd == "modes":
            out = request("GET", "/browsers/modes")
        elif args.cmd == "active":
            out = request("GET", "/browsers/active")
        elif args.cmd == "list":
            out, page = [], 1
            while True:
                resp = request(
                    "GET",
                    "/profiles",
                    page=page,
                    per_page=100,
                    search=args.search,
                    group=args.group,
                    status=args.status,
                )
                out += resp["profiles"]
                if not resp["has_next"]:
                    break
                page += 1
        elif args.cmd == "get":
            out = resolve(args.profile)
        elif args.cmd == "create":
            body: dict[str, Any] = json.loads(args.json)
            body["name"] = args.name
            if args.os:
                body.setdefault("browser_settings", {})["os"] = args.os
            if args.proxy:
                body["proxy_config"] = parse_proxy(args.proxy)
            if args.group:
                body["group"] = args.group
            if args.notes:
                body["notes"] = args.notes
            if args.protected:
                body["protected"] = True
            out = request("POST", "/profiles", body)
        elif args.cmd == "update":
            profile = resolve(args.profile)
            body = {"row_version": profile.get("row_version"), **json.loads(args.json)}
            out = request("PUT", f"/profiles/{profile['id']}", body)
        elif args.cmd == "protect":
            # Deliberately no "unprotect" command: lifting protection is the
            # user's decision, made with an explicit update.
            profile = resolve(args.profile)
            body = {"protected": True, "row_version": profile.get("row_version")}
            out = request("PUT", f"/profiles/{profile['id']}", body)
        elif args.cmd == "delete":
            out = request("DELETE", f"/profiles/{resolve(args.profile)['id']}")
        elif args.cmd == "launch":
            out = launch(args.profile, args.mode)
            out.pop("camoufox_options", None)  # launch internals; noise for an agent
            out.pop("browser_session_id", None)
        elif args.cmd == "close":
            out = request("POST", f"/profiles/{resolve(args.profile)['id']}/close")
    except CpmError as exc:
        print(
            json.dumps({"error": {"status": exc.status, "code": exc.code, "message": exc.message}})
        )
        sys.exit(1)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _main()
