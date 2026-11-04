"""Tests for per-profile VNC displays and how launches use them (no Xvnc started)."""

import base64

import pytest

from phasmid_pm.api.routes.viewer import _api_key_from_protocols
from phasmid_pm.core import browser_session as bs
from phasmid_pm.core.browser_session import BrowserLaunchError, BrowserSessionManager
from phasmid_pm.core.vnc import DEFAULT_GEOMETRY, display_geometry


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


class FakeBrowser:
    def on(self, *_args):
        pass


class FakeCamoufox:
    def __init__(self, options, fail=None):
        self.options = options
        self.fail = fail
        self.exits = 0

    async def start(self):
        if self.fail is not None:
            raise self.fail
        return FakeBrowser()

    async def __aexit__(self, *_exc):
        self.exits += 1


@pytest.fixture
def launches(monkeypatch):
    """Pretend Camoufox is installed; record the options of every launch."""
    made: list[FakeCamoufox] = []
    failure: list[Exception] = []

    def factory(**options):
        made.append(FakeCamoufox(options, failure[0] if failure else None))
        return made[-1]

    monkeypatch.setattr(bs, "CAMOUFOX_AVAILABLE", True)
    monkeypatch.setattr(bs, "AsyncCamoufox", factory)
    return made, failure


def test_geometry_follows_the_fingerprint_window():
    config = {
        "window.outerWidth": 1280,
        "window.outerHeight": 720,
        "screen.width": 1920,
        "screen.height": 1080,
    }
    assert display_geometry({"config": config}) == (1280, 720)


def test_geometry_falls_back_to_the_screen_then_the_window_then_the_default():
    options = {"config": {"screen.width": 2560, "screen.height": 1440}, "window": (800, 600)}
    assert display_geometry(options) == (2560, 1440)
    assert display_geometry({"window": (1280, 720)}) == (1280, 720)
    assert display_geometry({}) == DEFAULT_GEOMETRY


def test_api_key_is_read_from_the_subprotocol():
    encoded = base64.urlsafe_b64encode(b"s3cret/+key").decode().rstrip("=")
    assert _api_key_from_protocols(["binary", f"ppm-key.{encoded}"]) == "s3cret/+key"
    assert _api_key_from_protocols(["binary"]) is None
    assert _api_key_from_protocols(["ppm-key.!!!"]) is None


@pytest.mark.asyncio
async def test_headed_launch_draws_to_its_own_display(launches):
    made, _ = launches
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    options = {"config": {"screen.width": 1366, "screen.height": 768}}

    await manager.launch("p1", options)

    assert displays.started == [("p1", 1366, 768)]
    assert made[0].options["env"]["DISPLAY"] == ":100"
    # The caller's options are echoed back in the API response; they stay untouched.
    assert "env" not in options
    assert manager.list_active()[0]["vnc"] is True


@pytest.mark.asyncio
async def test_closing_the_browser_stops_its_display(launches):
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)
    await manager.launch("p1", {})

    await manager.close("p1")

    assert displays.stopped == ["p1"]


@pytest.mark.asyncio
async def test_failed_launch_does_not_leak_its_display(launches):
    _, failure = launches
    failure.append(RuntimeError("no browser"))
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)

    with pytest.raises(BrowserLaunchError):
        await manager.launch("p1", {})

    assert displays.stopped == ["p1"]
    assert not manager.is_live("p1")


@pytest.mark.asyncio
async def test_headless_launch_needs_no_display(launches):
    made, _ = launches
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)

    await manager.launch("p1", {"headless": True})

    assert displays.started == []
    assert "env" not in made[0].options
    assert manager.list_active()[0]["vnc"] is False


@pytest.mark.asyncio
async def test_vnc_is_off_by_default(launches):
    made, _ = launches
    manager = BrowserSessionManager()

    await manager.launch("p1", {})

    assert "env" not in made[0].options
    assert manager.list_active()[0]["vnc"] is False


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
    made, _ = launches
    displays = FakeDisplays()
    displays.enabled = False
    manager = BrowserSessionManager(vnc=displays)

    await manager.launch("p1", {}, mode="virtual")

    assert made[0].options["env"]["DISPLAY"] == ":100"
    assert manager.list_active()[0]["mode"] == "virtual"


@pytest.mark.asyncio
async def test_virtual_mode_hides_the_desktop_compositor(launches, monkeypatch):
    """Firefox prefers Wayland whenever it can reach one, which would put the
    window on the desktop and leave the VNC display black."""
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    made, _ = launches
    manager = BrowserSessionManager(vnc=FakeDisplays())

    await manager.launch("p1", {}, mode="virtual")

    assert "WAYLAND_DISPLAY" not in made[0].options["env"]


@pytest.mark.asyncio
async def test_headless_mode_overrides_a_headed_request(launches):
    made, _ = launches
    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)

    await manager.launch("p1", {"headless": False}, mode="headless")

    assert made[0].options["headless"] is True
    assert displays.started == []


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


def test_macos_and_windows_always_have_a_window_but_no_virtual_display(monkeypatch):
    monkeypatch.setattr(bs.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)

    by_mode = {m["mode"]: m for m in bs.available_modes(vnc_default=False)["modes"]}

    assert by_mode["window"]["available"] is True
    assert by_mode["virtual"]["available"] is False
