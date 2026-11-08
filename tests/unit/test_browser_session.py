"""Tests for the browser session layer, against a fake browser binary.

The fake (tests/fakes.py) is a real child process, so spawning, the wait for
DevToolsActivePort, process-group signals and the exit monitor are all the
production code paths; only Chrome itself is missing.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from phasmid_profile_manager.core import browser_session as bs
from phasmid_profile_manager.core.browser_session import (
    BrowserLaunchError,
    BrowserSessionManager,
    LaunchPlan,
    display_args,
    reap_orphan,
    resolve_display_mode,
    window_env,
)
from tests.fakes import write_fake_chrome


@pytest.fixture
def chrome(tmp_path) -> Path:
    return write_fake_chrome(tmp_path)


def make_plan(tmp_path: Path, chrome: Path, name: str = "p1", **env: str) -> LaunchPlan:
    run_dir = tmp_path / "run" / name
    run_dir.mkdir(parents=True)
    udd = tmp_path / "profiles" / name
    udd.mkdir(parents=True, exist_ok=True)
    return LaunchPlan(
        argv=[str(chrome), f"--user-data-dir={udd}"],
        user_data_dir=udd,
        run_dir=run_dir,
        geometry=(1600, 900),
        env=dict(env),
    )


def launched_argv(plan: LaunchPlan) -> dict:
    return json.loads((plan.user_data_dir / "argv.json").read_text())


async def wait_gone(manager: BrowserSessionManager, profile_id: str) -> None:
    for _ in range(100):
        if not manager.is_running(profile_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{profile_id} is still running")


# -- display modes -------------------------------------------------------------


def test_an_explicit_mode_wins():
    assert resolve_display_mode("window", True, True) == "window"


def test_headless_without_a_mode_stays_headless():
    assert resolve_display_mode(None, True, True) == "headless"


def test_a_headed_launch_follows_the_vnc_default():
    assert resolve_display_mode(None, False, True) == "virtual"
    assert resolve_display_mode(None, False, False) == "window"


def test_an_unknown_mode_is_refused():
    with pytest.raises(BrowserLaunchError, match="Unknown display mode"):
        resolve_display_mode("fullscreen", False, False)


def test_headless_adds_the_switch():
    switches, env = display_args("headless", {"DISPLAY": ":0"})
    assert switches == ["--headless"]
    assert env == {"DISPLAY": ":0"}


def test_a_virtual_display_keeps_chrome_off_wayland():
    switches, env = display_args("virtual", {"WAYLAND_DISPLAY": "wayland-1", "HOME": "/h"})
    assert switches == ["--ozone-platform=x11"]
    assert "WAYLAND_DISPLAY" not in env


def test_a_window_needs_a_desktop():
    with pytest.raises(BrowserLaunchError, match="No desktop session"):
        window_env({})
    assert window_env({"DISPLAY": ":1"})["DISPLAY"] == ":1"


def test_a_dangling_wayland_name_falls_back_to_x11(tmp_path):
    env = window_env(
        {"WAYLAND_DISPLAY": "wayland-9", "XDG_RUNTIME_DIR": str(tmp_path), "DISPLAY": ":0"}
    )
    assert "WAYLAND_DISPLAY" not in env


# -- launching -----------------------------------------------------------------


async def test_a_launch_reads_the_cdp_port_chrome_wrote(tmp_path, chrome):
    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome)
    session = await manager.launch("p1", plan, mode="headless")
    try:
        assert manager.is_running("p1")
        assert session.cdp_endpoint == "ws://127.0.0.1:1/devtools/browser/fake"
        assert session.info()["mode"] == "headless"
        assert session.info()["cdp"] is True
        assert "--headless" in launched_argv(plan)["argv"]
        assert (plan.run_dir / "pid").read_text() == str(session.process_id)
        # A session of its own, so the whole group can be signalled.
        assert os.getpgid(session.process_id) == session.process_id
    finally:
        await manager.close_all()


async def test_a_stale_port_file_is_not_mistaken_for_the_new_browser(tmp_path, chrome, monkeypatch):
    monkeypatch.setattr(bs, "STARTUP_TIMEOUT", 1.0)
    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome, FAKE_CHROME="hang")
    (plan.user_data_dir / "DevToolsActivePort").write_text("9\n/devtools/browser/old\n")
    with pytest.raises(BrowserLaunchError, match="did not open its debugging port"):
        await manager.launch("p1", plan, mode="headless")
    assert not manager.is_live("p1")
    assert not plan.run_dir.exists(), "a failed launch must not leave credentials behind"


async def test_a_browser_that_exits_at_startup_reports_why(tmp_path, chrome):
    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome, FAKE_CHROME="crash")
    with pytest.raises(BrowserLaunchError, match="profile refused"):
        await manager.launch("p1", plan, mode="headless")
    assert not manager.is_running("p1")
    assert not manager.is_live("p1")
    assert not plan.run_dir.exists()


async def test_launching_a_profile_twice_reuses_the_running_browser(tmp_path, chrome):
    manager = BrowserSessionManager()
    first = await manager.launch("p1", make_plan(tmp_path, chrome), mode="headless")
    try:
        again = await manager.launch("p1", make_plan(tmp_path, chrome, "p1b"), mode="headless")
        assert again is first
    finally:
        await manager.close_all()


async def test_a_virtual_display_is_started_and_named_in_the_environment(tmp_path, chrome):
    class FakeDisplays:
        enabled = True

        def __init__(self):
            self.started: list[tuple[str, int, int]] = []
            self.stopped: list[str] = []

        async def start(self, profile_id, width, height):
            self.started.append((profile_id, width, height))
            return type("D", (), {"name": ":123"})()

        async def stop(self, profile_id):
            self.stopped.append(profile_id)

        def get(self, profile_id):
            return object() if profile_id not in self.stopped else None

    displays = FakeDisplays()
    manager = BrowserSessionManager(vnc=displays)  # type: ignore[arg-type]
    plan = make_plan(tmp_path, chrome, WAYLAND_DISPLAY="wayland-0")
    session = await manager.launch("p1", plan, mode="virtual")
    seen = launched_argv(plan)
    assert displays.started == [("p1", 1600, 900)]
    assert seen["display"] == ":123"
    assert seen["wayland"] is None
    assert "--ozone-platform=x11" in seen["argv"]
    assert session.info()["vnc"] is True
    await manager.close("p1")
    assert displays.stopped == ["p1"], "the display goes once the browser is gone"


# -- closing -------------------------------------------------------------------


async def test_close_ends_the_process_and_removes_the_run_directory(tmp_path, chrome):
    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome)
    session = await manager.launch("p1", plan, mode="headless")
    pid = session.process_id
    assert await manager.close("p1") is True
    assert session.process.returncode is not None
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    assert not plan.run_dir.exists()
    assert await manager.close("p1") is False


async def test_a_browser_that_ignores_sigterm_is_killed(tmp_path, chrome, monkeypatch):
    monkeypatch.setattr(bs, "TERM_TIMEOUT", 0.3)
    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome, FAKE_CHROME="ignore-term")
    session = await manager.launch("p1", plan, mode="headless")
    await manager.close("p1")
    assert session.process.returncode == -signal.SIGKILL


async def test_terminate_is_idempotent(tmp_path, chrome):
    manager = BrowserSessionManager()
    session = await manager.launch("p1", make_plan(tmp_path, chrome), mode="headless")
    await session.terminate()
    await session.terminate()
    assert session.process.returncode is not None
    await manager.close("p1")


async def test_a_browser_closed_from_outside_is_noticed_once(tmp_path, chrome):
    """Someone closes the window: the process exits and the session goes with it."""
    exits: list[str] = []

    async def on_exit(profile_id: str) -> None:
        exits.append(profile_id)

    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome)
    session = await manager.launch("p1", plan, on_exit=on_exit, mode="headless")
    os.kill(session.process_id, signal.SIGTERM)
    await wait_gone(manager, "p1")
    await asyncio.sleep(0.1)
    assert exits == ["p1"]
    assert not plan.run_dir.exists()


async def test_an_exit_handler_that_raises_does_not_break_teardown(tmp_path, chrome):
    async def on_exit(_profile_id: str) -> None:
        raise RuntimeError("handler bug")

    manager = BrowserSessionManager()
    plan = make_plan(tmp_path, chrome)
    session = await manager.launch("p1", plan, on_exit=on_exit, mode="headless")
    os.kill(session.process_id, signal.SIGTERM)
    await wait_gone(manager, "p1")


async def test_close_all_closes_every_browser(tmp_path, chrome):
    manager = BrowserSessionManager()
    for name in ("a", "b"):
        await manager.launch(name, make_plan(tmp_path, chrome, name), mode="headless")
    assert await manager.close_all() == 2
    assert manager.list_active() == []


# -- orphans -------------------------------------------------------------------


def test_an_orphan_naming_its_run_directory_is_killed(tmp_path, chrome):
    run_dir = tmp_path / "run" / "p1"
    run_dir.mkdir(parents=True)
    udd = tmp_path / "udd"
    process = subprocess.Popen(
        [str(chrome), f"--user-data-dir={udd}", f"--phasmid-profile={run_dir}/profile.json"],
        start_new_session=True,
    )
    (run_dir / "pid").write_text(str(process.pid))
    assert reap_orphan(run_dir) is True
    assert process.wait(timeout=5) == -signal.SIGKILL
    assert not run_dir.exists()


def test_a_reused_pid_is_never_killed(tmp_path):
    run_dir = tmp_path / "run" / "p1"
    run_dir.mkdir(parents=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (run_dir / "pid").write_text(str(bystander.pid))
        assert reap_orphan(run_dir) is False
        assert bystander.poll() is None, "a process that does not name the run dir is not ours"
        assert not run_dir.exists()
    finally:
        bystander.kill()
        bystander.wait()
