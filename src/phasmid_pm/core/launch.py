"""Turn a profile and its pinned machine into a Phasmid command line.

    chrome --phasmid-profile=<run>/profile.json --user-data-dir=<profile dir>
           --remote-debugging-port=0 --no-first-run --no-default-browser-check

With a proxy, the browser is started through Phasmid's ``tools/proxyroute``:
Chrome ignores credentials in ``--proxy-server`` and would prompt for them, so
proxyroute relays authenticated proxies through a loopback SOCKS5 listener and
hands Chrome a PAC script. Its arguments, credentials included, go in a file
(``@<run>/proxy-args``, mode 0600) so they never appear in ``ps``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .browser_install import Installation
from .browser_session import BrowserLaunchError, LaunchPlan
from .models import Profile, ProxyConfig, ProxyType
from .proxy_check import proxy_url
from .vnc import display_geometry

PROFILE_FILE = "profile.json"
PROXY_ARGS_FILE = "proxy-args"


def has_gpu_device() -> bool:
    """Whether a GPU device node is reachable (not the case in a plain container)."""
    return os.path.exists("/dev/dri") or os.path.exists("/dev/nvidia0")


def launch_profile(profile: Profile, pin: dict[str, Any]) -> dict[str, Any]:
    """The profile JSON the browser reads: the pin plus per-launch preferences."""
    data = copy.deepcopy(pin)
    webrtc = data.setdefault("network", {}).setdefault("webrtc", {})
    webrtc["ip_handling_policy"] = profile.browser_settings.webrtc_policy
    return data


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


def proxy_switches(proxy: ProxyConfig) -> tuple[list[str], list[str]]:
    """(proxyroute arguments, Chrome switches) for a proxy.

    SOCKS4 has no credentials to relay and proxyroute does not speak it, so it
    goes to Chrome directly; everything else goes through proxyroute.
    """
    if proxy.type == ProxyType.SOCKS4:
        if proxy.username or proxy.password:
            raise BrowserLaunchError("SOCKS4 proxies cannot authenticate; use SOCKS5 or HTTP.")
        return [], [f"--proxy-server=socks4://{proxy.server}"]
    return [f"--proxy-default={proxy_url(proxy)}"], []


def build_plan(
    installation: Installation,
    profile: Profile,
    pin: dict[str, Any],
    user_data_dir: Path,
    run_root: Path,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> LaunchPlan:
    """Write the session's files and return the command that starts the browser."""
    run_dir = run_root / profile.id
    shutil.rmtree(run_dir, ignore_errors=True)
    run_dir.mkdir(parents=True, mode=0o700)
    profile_path = run_dir / PROFILE_FILE
    _write_private(profile_path, json.dumps(launch_profile(profile, pin), indent=1))

    chrome = [
        str(installation.binary),
        f"--phasmid-profile={profile_path}",
        f"--user-data-dir={user_data_dir}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        # Cookies and saved passwords are encrypted with a key in the desktop
        # keyring by default, which would tie a profile directory to this
        # login: an exported profile would arrive logged out, and a profile
        # opened from Docker and from the desktop would not share sessions.
        # Pages cannot observe the password store.
        "--password-store=basic",
    ]
    # As the Phasmid Docker image does: without a GPU device, start the GPU
    # process on SwiftShader directly. An attempt at a device it cannot open
    # leaves it without the hardware decode support a profile may claim.
    if not has_gpu_device():
        chrome.append("--disable-gpu")
    chrome.extend(extra_args or [])

    argv = chrome
    if profile.proxy is not None:
        route, switches = proxy_switches(profile.proxy)
        chrome.extend(switches)
        if route:
            args_path = run_dir / PROXY_ARGS_FILE
            _write_private(args_path, "\n".join(route) + "\n")
            argv = [sys.executable, str(installation.proxyroute), f"@{args_path}", "--", *chrome]

    return LaunchPlan(
        argv=argv,
        user_data_dir=user_data_dir,
        run_dir=run_dir,
        geometry=display_geometry(pin),
        headless=headless,
    )
