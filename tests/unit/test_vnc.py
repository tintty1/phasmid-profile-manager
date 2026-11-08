"""Tests for per-profile VNC displays and how launches use them (no Xvnc started)."""

import base64
import json

import pytest

from phasmid_profile_manager.api.routes.viewer import _api_key_from_protocols
from phasmid_profile_manager.core import browser_session as bs
from phasmid_profile_manager.core.browser_session import (
    BrowserLaunchError,
    BrowserSessionManager,
    LaunchPlan,
)
from phasmid_profile_manager.core.vnc import DEFAULT_GEOMETRY, display_geometry
from tests.fakes import write_fake_chrome


class FakeDisplay:
    def __init__(self, number: int):
        self.name = f":{number}"


class FakeDisplays:
    """Stands in for VncDisplays, recording what was started and stopped."""

    enabled = True

    def __init__(self):
        self.started: list[tuple[str, int, int]] = []
        self.stopped: list[str] = []
        self.live: dict[str, FakeDisplay] = {}

    async def start(self, profile_id, width, height):
        self.started.append((profile_id, width, height))
        self.live[profile_id] = FakeDisplay(100 + len(self.started) - 1)
        return self.live[profile_id]

    async def stop(self, profile_id):
        self.stopped.append(profile_id)
        self.live.pop(profile_id, None)

    def get(self, profile_id):
        return self.live.get(profile_id)


@pytest.fixture
def launches(tmp_path, monkeypatch):
    """Launch plans for the fake browser; ``seen(plan)`` is what it was started with."""
    chrome = write_fake_chrome(tmp_path)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    def plan(name: str = "p1", geometry=(1920, 1080), crash: bool = False) -> LaunchPlan:
        run_dir = tmp_path / "run" / name
        run_dir.mkdir(parents=True)
        udd = tmp_path / "profiles" / name
        udd.mkdir(parents=True)
        env = {"FAKE_CHROME": "crash"} if crash else {}
        return LaunchPlan([str(chrome), f"--user-data-dir={udd}"], udd, run_dir, geometry, env=env)

    return plan


def seen(plan: LaunchPlan) -> dict:
    return json.loads((plan.user_data_dir / "argv.json").read_text())


def test_geometry_follows_the_profile_window():
    pin = {
        "display": {
            "width": 1920,
            "height": 1080,
            "window": {"outer_width": 1280, "outer_height": 720},
        }
    }
    assert display_geometry(pin) == (1280, 720)


def test_geometry_falls_back_to_the_screen_then_the_default():
    assert display_geometry({"display": {"width": 2560, "height": 1440}}) == (2560, 1440)
    assert display_geometry({}) == DEFAULT_GEOMETRY
    assert display_geometry(None) == DEFAULT_GEOMETRY


def test_api_key_is_read_from_the_subprotocol():
    encoded = base64.urlsafe_b64encode(b"s3cret/+key").decode().rstrip("=")
    assert _api_key_from_protocols(["binary", f"ppm-key.{encoded}"]) == "s3cret/+key"
    assert _api_key_from_protocols(["binary"]) is None
    assert _api_key_from_protocols(["ppm-key.!!!"]) is None


@pytest.mark.asyncio
async def test_headed_launch_draws_to_its_own_display(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    plan = launches(geometry=(1366, 768))

    await manager.launch("p1", plan)

    assert displays.started == [("p1", 1366, 768)]
    assert seen(plan)["display"] == ":100"
    assert manager.list_active()[0]["vnc"] is True
    await manager.close_all()


@pytest.mark.asyncio
async def test_closing_the_browser_stops_its_display(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    await manager.launch("p1", launches())

    await manager.close("p1")

    assert displays.stopped == ["p1"]


@pytest.mark.asyncio
async def test_failed_launch_does_not_leak_its_display(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)

    with pytest.raises(BrowserLaunchError):
        await manager.launch("p1", launches(crash=True))

    assert displays.stopped == ["p1"]
    assert not manager.is_live("p1")


@pytest.mark.asyncio
async def test_headless_launch_needs_no_display(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    plan = launches()
    plan.headless = True

    await manager.launch("p1", plan)

    assert displays.started == []
    assert "--headless" in seen(plan)["argv"]
    assert manager.list_active()[0]["vnc"] is False
    await manager.close_all()


@pytest.mark.asyncio
async def test_vnc_is_off_by_default(launches, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    manager = BrowserSessionManager()
    plan = launches()

    await manager.launch("p1", plan)

    assert seen(plan)["display"] == ":0", "the browser inherits this process's display"
    assert manager.list_active()[0]["vnc"] is False
    await manager.close_all()


# -- Display modes ------------------------------------------------------------


def test_an_explicit_mode_wins_over_headless_and_the_vnc_default():
    assert bs.resolve_display_mode("window", True, True) == "window"
    assert bs.resolve_display_mode(None, True, True) == "headless"
    assert bs.resolve_display_mode(None, False, True) == "virtual"
    assert bs.resolve_display_mode(None, False, False) == "window"
    with pytest.raises(BrowserLaunchError, match="Unknown display mode"):
        bs.resolve_display_mode("kiosk", False, False)


@pytest.mark.asyncio
async def test_virtual_mode_works_without_vnc_as_the_default(launches):
    """PPM_VNC only picks the default; any instance can be asked for a display."""
    displays = FakeDisplays()
    displays.enabled = False
    manager = BrowserSessionManager(vnc=displays)
    plan = launches()

    await manager.launch("p1", plan, mode="virtual")

    assert seen(plan)["display"] == ":100"
    assert manager.list_active()[0]["mode"] == "virtual"
    await manager.close_all()


@pytest.mark.asyncio
async def test_virtual_mode_hides_the_desktop_compositor(launches, monkeypatch):
    """Chrome prefers Wayland whenever it can reach one, which would put the
    window on the desktop and leave the VNC display black."""
    manager = BrowserSessionManager(vnc=FakeDisplays())
    plan = launches()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    await manager.launch("p1", plan, mode="virtual")

    assert seen(plan)["wayland"] is None
    assert "--ozone-platform=x11" in seen(plan)["argv"]
    await manager.close_all()


@pytest.mark.asyncio
async def test_headless_mode_overrides_a_headed_request(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    plan = launches()

    await manager.launch("p1", plan, mode="headless")

    assert "--headless" in seen(plan)["argv"]
    assert displays.started == []
    await manager.close_all()


def test_window_mode_prefers_a_reachable_wayland_socket(tmp_path):
    (tmp_path / "wayland-1").touch()
    env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(tmp_path), "DISPLAY": ":0"}

    assert bs.window_env(env)["WAYLAND_DISPLAY"] == "wayland-1"


def test_window_mode_falls_back_to_x11_when_wayland_is_unreachable(tmp_path):
    env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(tmp_path), "DISPLAY": ":0"}

    shaped = bs.window_env(env)

    assert "WAYLAND_DISPLAY" not in shaped
    assert shaped["DISPLAY"] == ":0"


def test_window_mode_without_a_desktop_says_what_to_do_instead():
    with pytest.raises(BrowserLaunchError, match="virtual"):
        bs.window_env({"PATH": "/usr/bin"})


def test_the_launch_menu_lists_every_mode_with_why_one_is_off(monkeypatch):
    monkeypatch.setattr(bs.sys, "platform", "linux")
    monkeypatch.setattr(bs.shutil, "which", lambda _name: None)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    modes = bs.available_modes(vnc_default=True)

    assert modes["default"] == "virtual"
    by_mode = {m["mode"]: m for m in modes["modes"]}
    assert list(by_mode) == ["headless", "virtual", "window"]
    assert by_mode["headless"]["available"] is True
    assert by_mode["virtual"]["available"] is False
    assert "Xvnc" in by_mode["virtual"]["reason"]
    assert by_mode["window"]["available"] is False
    assert "docker-compose.desktop.yml" in by_mode["window"]["reason"]


def test_a_desktop_and_xvnc_make_every_mode_available(monkeypatch):
    monkeypatch.setattr(bs.sys, "platform", "linux")
    monkeypatch.setattr(bs.shutil, "which", lambda _name: "/usr/bin/Xvnc")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")

    modes = bs.available_modes(vnc_default=False)

    assert modes["default"] == "window"
    assert all(m["available"] for m in modes["modes"])
