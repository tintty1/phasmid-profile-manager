"""Stand-ins for Phasmid in tests: a fake browser binary and a fake genprofile.

The fake browser is a real child process, so the session code under test
spawns, waits for and signals it exactly as it does Chrome. It understands the
switches the session code relies on and nothing else:

- ``--user-data-dir=<dir>``: it writes ``DevToolsActivePort`` there (unless told
  not to) and its own argv to ``argv.json`` beside it, for assertions.
- ``FAKE_CHROME`` in the environment picks a behaviour: ``ok`` (the default;
  run until signalled), ``crash`` (exit 3 at once, with a message on stderr),
  ``hang`` (never write the port file), ``ignore-term`` (survive SIGTERM).
"""

from __future__ import annotations

import copy
import hashlib
import sys
import textwrap
from pathlib import Path
from typing import Any

FAKE_CHROME_SOURCE = textwrap.dedent(
    """\
    import json, os, signal, sys, time
    mode = os.environ.get("FAKE_CHROME", "ok")
    udd = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--user-data-dir="))
    os.makedirs(udd, exist_ok=True)
    with open(os.path.join(udd, "argv.json"), "w") as f:
        json.dump({"argv": sys.argv, "display": os.environ.get("DISPLAY"),
                   "wayland": os.environ.get("WAYLAND_DISPLAY")}, f)
    if mode == "crash":
        print("fake chrome: profile refused", file=sys.stderr)
        sys.exit(3)
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if mode != "hang":
        with open(os.path.join(udd, "DevToolsActivePort"), "w") as f:
            f.write("1\\n/devtools/browser/fake\\n")
    while True:
        time.sleep(0.05)
    """
)


def write_fake_chrome(directory: Path) -> Path:
    """An executable fake ``chrome`` in ``directory``."""
    path = directory / "chrome"
    path.write_text(f"#!{sys.executable}\n" + FAKE_CHROME_SOURCE)
    path.chmod(0o755)
    return path


def _int(seed: str, salt: str, low: int, high: int) -> int:
    digest = hashlib.sha256(f"{seed}:{salt}".encode()).digest()
    return low + int.from_bytes(digest[:4], "big") % (high - low + 1)


def fake_pin(
    os: str = "linux",
    seed: str = "0" * 32,
    timezone: str = "Europe/Berlin",
    languages: list[str] | None = None,
    public_ip: str | None = None,
    gpu: str | None = None,
) -> dict[str, Any]:
    """A small Phasmid-shaped profile whose hardware depends on the seed only."""
    vendor, device = (gpu or "0x10de:0x2504").split(":")
    width = [1920, 2560, 1366, 1600][_int(seed, "screen", 0, 3)]
    height = {1920: 1080, 2560: 1440, 1366: 768, 1600: 900}[width]
    pin: dict[str, Any] = {
        "schema_version": 1,
        "id": f"gen-{os}-{width}x{height}-{seed[:8]}",
        "seed": seed,
        "captured": {"note": f"fake, placed at {public_ip or timezone}"},
        "os": {
            "family": os,
            "version": "11" if os == "windows" else "arch",
            "navigator_platform": "Win32" if os == "windows" else "Linux x86_64",
        },
        "hardware": {
            "concurrency": _int(seed, "cores", 2, 32),
            "device_memory": 8,
            "max_touch_points": 0,
        },
        "display": {
            "width": width,
            "height": height,
            "device_pixel_ratio": 1,
            "window": {"outer_width": width, "outer_height": height - 40},
        },
        "gpu": {
            "vendor_id": vendor,
            "device_id": device,
            "webgl": {"unmasked_renderer": f"ANGLE (fake {device})"},
        },
        "fonts": {"families": ["DejaVu Sans", "Liberation Serif"]},
        "locale": {"languages": languages or ["en-US", "en"], "timezone": timezone},
        "network": {"webrtc": {"ip_handling_policy": "default"}},
        "automation": {"report_headful": True},
    }
    if public_ip:
        pin["network"]["webrtc"]["public_ip"] = public_ip
        pin["locale"]["geolocation"] = {"latitude": 52.5, "longitude": 13.4, "accuracy": 50000}
    return copy.deepcopy(pin)


# Where the fake genprofile places an exit address: (country, timezone, languages).
FAKE_EXITS = {
    "203.0.113.10": ("DE", "Europe/Berlin", ["de-DE", "de", "en-US", "en"]),
    "203.0.113.20": ("JP", "Asia/Tokyo", ["ja", "en-US", "en"]),
    "198.51.100.7": ("US", "America/New_York", ["en-US", "en"]),
}

FAKE_GPUS = [
    {
        "os": "linux",
        "gpu": "0x10de:0x2504",
        "renderer": "ANGLE (NVIDIA GeForce RTX 3060)",
        "weight": 3,
    },
    {
        "os": "linux",
        "gpu": "0x10de:0x2482",
        "renderer": "ANGLE (NVIDIA GeForce RTX 3070 Ti)",
        "weight": 2,
    },
    {
        "os": "windows",
        "gpu": "0x8086:0x3185",
        "renderer": "ANGLE (Intel UHD Graphics 600)",
        "weight": 1,
    },
]


class FakeGenprofile:
    """Answers run_genprofile() the way the real driver does, without a source tree."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None

    def __call__(
        self, command: str, args: dict[str, Any] | None = None, installation: Any = None
    ) -> dict[str, Any]:
        from phasmid_pm.core.browser_install import GenprofileError

        args = dict(args or {})
        self.calls.append((command, args))
        if self.fail:
            raise GenprofileError(self.fail)
        if command == "gpus":
            return {"gpus": copy.deepcopy(FAKE_GPUS)}
        if command == "locate":
            country, zone, languages = FAKE_EXITS.get(args["ip"], ("ZZ", "UTC", ["en-US", "en"]))
            return {
                "location": {
                    "ip": args["ip"],
                    "country": country,
                    "city": "Somewhere",
                    "timezone": zone,
                    "latitude": 52.5,
                    "longitude": 13.4,
                    "accuracy": 50000.0,
                    "languages": languages,
                }
            }
        if command == "generate":
            if args.get("gpu") and not any(
                g["gpu"] == args["gpu"] and g["os"] == args["os"] for g in FAKE_GPUS
            ):
                raise GenprofileError(
                    f"GenerationError: no capture for os={args['os']} backs gpu {args['gpu']}"
                )
            if "exit_ip" in args:
                _, zone, languages = FAKE_EXITS.get(args["exit_ip"], ("ZZ", "UTC", ["en-US", "en"]))
                pin = fake_pin(
                    args["os"],
                    args["seed"],
                    zone,
                    args.get("languages") or languages,
                    args["exit_ip"],
                    args.get("gpu"),
                )
            else:
                pin = fake_pin(
                    args["os"],
                    args["seed"],
                    args["timezone"],
                    args.get("languages"),
                    None,
                    args.get("gpu"),
                )
            return {"profile": pin}
        raise AssertionError(f"unexpected genprofile command {command}")

    def generated(self) -> list[dict[str, Any]]:
        return [args for command, args in self.calls if command == "generate"]


async def launch_through_api(client: Any, name: str) -> tuple[str, Any]:
    """Create a profile and launch it (the fake browser) over the API.

    Returns the profile id and its BrowserSession, so a test can see whether
    the process was ended.
    """
    from phasmid_pm.api.dependencies import get_profile_manager

    created = await client.post("/api/profiles", json={"name": name})
    profile_id = created.json()["id"]
    launched = await client.post(f"/api/profiles/{profile_id}/launch", json={"mode": "headless"})
    assert launched.status_code == 200, launched.text
    return profile_id, get_profile_manager().browser_sessions.active_sessions[profile_id]
