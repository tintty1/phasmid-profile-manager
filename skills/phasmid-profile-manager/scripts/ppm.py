#!/usr/bin/env python3
"""Phasmid Profile Manager helper: profile CRUD from the shell, CDP from Python.

Configuration comes from the environment:
  PPM_URL      base URL of the manager (default http://127.0.0.1:8000)
  PPM_API_KEY  sent as X-API-Key when set

Shell (stdlib only, prints JSON):
  ppm.py modes                         which launch modes this server offers
  ppm.py list [--search TEXT] [--group ID] [--status active]
  ppm.py get ID_OR_NAME
  ppm.py create NAME [--os linux|windows] [--gpu 0x10de:0x2504]
                     [--proxy socks5://user:pass@host:port]
                     [--group ID] [--notes TEXT] [--protected] [--json '{...extra fields...}']
  ppm.py gpus [--os linux|windows]                     GPUs a new machine may claim
  ppm.py machine ID_OR_NAME                            the whole Phasmid profile JSON
  ppm.py protect ID_OR_NAME                            (refuse delete / new machine)
  ppm.py update ID_OR_NAME --json '{"notes": "..."}'   (sends row_version for you)
  ppm.py clone ID_OR_NAME NEW_NAME [--same-machine] [--no-data]
                                                       (new hardware unless --same-machine;
                                                        cookies/logins copied unless --no-data)
  ppm.py delete ID_OR_NAME                             (moves to Trash; recoverable)
  ppm.py trash                                         profiles in Trash
  ppm.py restore ID_OR_NAME                            back out of Trash
  ppm.py purge ID_OR_NAME --yes                        erase a Trash entry for good
  ppm.py destroy ID_OR_NAME --yes                      close + Trash + erase, in one go
  ppm.py launch ID_OR_NAME [--mode headless|virtual|window]
  ppm.py close ID_OR_NAME
  ppm.py active
  ppm.py downloads ID_OR_NAME                          files the profile downloaded, newest first
  ppm.py fetch ID_OR_NAME FILE [-o PATH]               copy one to this machine (default ./FILE)
  ppm.py rm-download ID_OR_NAME FILE

Python (needs playwright; any recent version speaks CDP):
  from ppm import open_profile, fetch_download
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

BASE = os.environ.get("PPM_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE}/api/v1"
KEY = os.environ.get("PPM_API_KEY")


class PpmError(RuntimeError):
    """An API error, carrying the server's status and error code."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


def request(method: str, path: str, body: Any = None, raw: bool = False, **query: Any) -> Any:
    """Call the API and return the decoded JSON body (bytes with raw); raise PpmError."""
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
            content = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read()).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        raise PpmError(
            exc.code, error.get("code", "http_error"), error.get("message", str(exc))
        ) from None
    except urllib.error.URLError as exc:
        raise PpmError(0, "unreachable", f"cannot reach {BASE}: {exc.reason}") from None
    if raw:
        return content
    return json.loads(content) if content else None


def resolve(id_or_name: str) -> dict[str, Any]:
    """A profile by id, or else by exact name (refusing an ambiguous name)."""
    with contextlib.suppress(PpmError):
        return request("GET", f"/profiles/{urllib.parse.quote(id_or_name)}")
    found = [
        p
        for p in request("GET", "/profiles", search=id_or_name, per_page=100)["profiles"]
        if p["name"] == id_or_name
    ]
    if not found:
        raise PpmError(404, "not_found", f"no profile with id or name {id_or_name!r}")
    if len(found) > 1:
        ids = ", ".join(p["id"] for p in found)
        raise PpmError(409, "ambiguous", f"{len(found)} profiles are named {id_or_name!r}: {ids}")
    return found[0]


def resolve_trashed(id_or_name: str) -> dict[str, Any]:
    """A profile in Trash by id, or else by exact name."""
    trashed = request("GET", "/trash/profiles")["data"]["profiles"]
    found = [p for p in trashed if p["id"] == id_or_name] or [
        p for p in trashed if p["name"] == id_or_name
    ]
    if not found:
        raise PpmError(404, "not_found", f"no profile in Trash with id or name {id_or_name!r}")
    if len(found) > 1:
        ids = ", ".join(p["id"] for p in found)
        raise PpmError(
            409, "ambiguous", f"{len(found)} trashed profiles are named {id_or_name!r}: {ids}"
        )
    return found[0]


def destroy(id_or_name: str) -> dict[str, Any]:
    """Close the profile if it runs, move it to Trash and erase it. Irreversible.

    The profile's directory (cookies, logins, history) is deleted with it.
    Protected profiles are refused (409 protected) by the server.
    """
    profile = resolve(id_or_name)
    request("POST", f"/profiles/{profile['id']}/close")
    request("DELETE", f"/profiles/{profile['id']}")
    request("DELETE", f"/trash/profiles/{profile['id']}", confirm="true")
    return {"destroyed": profile["id"], "name": profile["name"]}


def list_downloads(id_or_name: str) -> list[dict[str, Any]]:
    """The profile's downloaded files, newest first: name, size, modified, complete."""
    return request("GET", f"/profiles/{resolve(id_or_name)['id']}/downloads")["files"]


def fetch_download(id_or_name: str, name: str, dest: str | None = None) -> str:
    """Copy one downloaded file from the server to ``dest`` (default ./name); returns the path.

    The only reliable way to get a download when the browser runs on another
    machine or in Docker: Playwright's download.save_as() reads a path on the
    agent's machine and, for a remote browser, quietly writes an empty file.
    """
    profile = resolve(id_or_name)
    quoted = urllib.parse.quote(name, safe="")
    data = request("GET", f"/profiles/{profile['id']}/downloads/{quoted}", raw=True)
    path = dest or name
    with open(path, "wb") as handle:
        handle.write(data)
    return path


async def wait_for_download(id_or_name: str, name: str, timeout: float = 120.0) -> dict[str, Any]:
    """Wait until the named file is in the profile's downloads and complete."""
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for entry in list_downloads(id_or_name):
            if entry["name"] == name and entry["complete"]:
                return entry
        await asyncio.sleep(0.5)
    raise PpmError(408, "timeout", f"{name!r} did not finish downloading in {timeout:.0f}s")


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
    cdp_endpoint, so launching first and connecting second is always safe.
    """
    profile = resolve(id_or_name)
    return request("POST", f"/profiles/{profile['id']}/launch", {"mode": mode} if mode else {})


@contextlib.asynccontextmanager
async def open_profile(id_or_name: str, mode: str | None = None, stop_on_exit: bool = False):
    """Launch a profile and yield (page, context, launch_info) driven over CDP.

    Headless unless ``mode`` says otherwise. Downloads go to the profile's own
    folder on the server; get them with fetch_download().

    The context is the profile's own persistent one - its cookies, storage and
    machine. Leaving the block disconnects; the browser keeps running unless
    stop_on_exit is True (or you call close()).
    """
    from playwright.async_api import async_playwright

    info = launch(id_or_name, mode or "headless")
    endpoint = info.get("cdp_endpoint")
    if not endpoint:
        raise PpmError(503, "no_cdp", f"{info['profile_id']} has no CDP endpoint: {info}")
    headers = {"x-api-key": KEY} if KEY else None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(endpoint, headers=headers)
            try:
                # Playwright points downloads at a temporary folder on *this*
                # machine; send them back to the profile's own folder on the
                # server, where fetch_download() can reach them from anywhere.
                if info.get("downloads_dir"):
                    cdp = await browser.new_browser_cdp_session()
                    await cdp.send(
                        "Browser.setDownloadBehavior",
                        {
                            "behavior": "allow",
                            "downloadPath": info["downloads_dir"],
                            "eventsEnabled": True,
                        },
                    )
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                yield page, context, info
            finally:
                await browser.close()  # over CDP this disconnects; the browser stays up
    finally:
        if stop_on_exit:
            with contextlib.suppress(PpmError):
                request("POST", f"/profiles/{info['profile_id']}/close")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("modes")
    sub.add_parser("active")
    sub.add_parser("gpus").add_argument("--os", choices=["linux", "windows"])
    sub.add_parser("trash")
    p = sub.add_parser("clone")
    p.add_argument("profile")
    p.add_argument("new_name")
    p.add_argument(
        "--no-data",
        action="store_true",
        help="start the clone as a fresh browser: no cookies, logins, storage or history",
    )
    p.add_argument(
        "--same-machine",
        action="store_true",
        help="copy the hardware too: the two profiles become provably one computer",
    )
    p = sub.add_parser("fetch")
    p.add_argument("profile")
    p.add_argument("file")
    p.add_argument("-o", "--output", help="where to write it (default ./FILE)")
    p = sub.add_parser("rm-download")
    p.add_argument("profile")
    p.add_argument("file")
    for name in ("purge", "destroy"):
        p = sub.add_parser(name)
        p.add_argument("profile")
        p.add_argument("--yes", action="store_true", help="confirm: this cannot be undone")
    p = sub.add_parser("list")
    p.add_argument("--search")
    p.add_argument("--group")
    p.add_argument("--status")
    for name in ("get", "delete", "close", "protect", "machine", "restore", "downloads"):
        sub.add_parser(name).add_argument("profile")
    p = sub.add_parser("create")
    p.add_argument("name")
    p.add_argument("--os", choices=["linux", "windows"])
    p.add_argument("--gpu", help="vendor_id:device_id, from `ppm.py gpus`")
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
        elif args.cmd == "gpus":
            out = request("GET", "/fingerprints/gpus", os=args.os)["data"]["gpus"]
        elif args.cmd == "machine":
            out = request("GET", f"/profiles/{resolve(args.profile)['id']}/machine")
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
            if args.gpu:
                body.setdefault("browser_settings", {})["gpu"] = args.gpu
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
        elif args.cmd == "clone":
            source = resolve(args.profile)
            body = {
                "new_name": args.new_name,
                "regenerate_fingerprint": not args.same_machine,
                "copy_data": not args.no_data,
            }
            out = request("POST", f"/profiles/{source['id']}/clone", body)
        elif args.cmd == "downloads":
            out = list_downloads(args.profile)
        elif args.cmd == "fetch":
            out = {"saved": fetch_download(args.profile, args.file, args.output)}
        elif args.cmd == "rm-download":
            quoted = urllib.parse.quote(args.file, safe="")
            out = request("DELETE", f"/profiles/{resolve(args.profile)['id']}/downloads/{quoted}")
        elif args.cmd == "trash":
            out = request("GET", "/trash/profiles")["data"]["profiles"]
        elif args.cmd == "restore":
            trashed = resolve_trashed(args.profile)
            out = request("POST", f"/trash/profiles/{trashed['id']}/restore")
        elif args.cmd in ("purge", "destroy"):
            if not args.yes:
                raise PpmError(
                    400, "unconfirmed", f"{args.cmd} erases the profile for good; pass --yes"
                )
            if args.cmd == "purge":
                trashed = resolve_trashed(args.profile)
                out = request("DELETE", f"/trash/profiles/{trashed['id']}", confirm="true")
            else:
                out = destroy(args.profile)
        elif args.cmd == "launch":
            out = launch(args.profile, args.mode)
            out.pop("launch", None)  # launch internals; noise for an agent
        elif args.cmd == "close":
            out = request("POST", f"/profiles/{resolve(args.profile)['id']}/close")
    except PpmError as exc:
        print(
            json.dumps({"error": {"status": exc.status, "code": exc.code, "message": exc.message}})
        )
        sys.exit(1)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _main()
