"""Browser session lifecycle: launch, monitor, and close Phasmid instances.

Phasmid is a normal Chromium binary, so a session is a process this module
starts itself, not a Playwright-owned browser: no driver adds its own switches
(``--enable-automation`` and the rest) to the command line, and the browser
looks like one a person opened. Automation attaches over CDP afterwards,
through ``--remote-debugging-port=0``: Chrome picks a free loopback port and
writes it to ``DevToolsActivePort`` in the user data directory.

The process is the browser (or proxyroute, its parent while it relays proxy
credentials), started in a session of its own so the whole group can be
signalled. Its exit is the close signal: a person closing the window, a
crash, and :meth:`BrowserSession.terminate` all end in the same place.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..config import get_settings
from .vnc import VncDisplays

if TYPE_CHECKING:
    # Type-only: importing StorageManager at runtime would be a cycle, since
    # database.py has no need of this module at all.
    from .database import StorageManager

ExitHandler = Callable[[str], Awaitable[None]]


class BrowserLaunchError(RuntimeError):
    """Raised when a Phasmid browser fails to launch."""


# Where a headed browser draws is chosen per launch, not per instance.
DISPLAY_MODES = ("headless", "virtual", "window")

# How long Chrome gets to write DevToolsActivePort. A cold start of a new
# profile directory under a software GPU takes a few seconds.
STARTUP_TIMEOUT = 45.0
# How long a graceful Browser.close gets before the process group is signalled,
# and how long SIGTERM gets before SIGKILL.
CLOSE_TIMEOUT = 10.0
TERM_TIMEOUT = 5.0
# In a session's run directory: the pid (and process group) of its browser.
PID_FILE = "pid"


@dataclass
class LaunchPlan:
    """Everything a launch needs, decided before the process starts."""

    # The command up to and excluding the display switches this module adds.
    argv: list[str]
    user_data_dir: Path
    # Where the session's private files go: the profile JSON, proxy arguments
    # and the browser log. Removed when the session ends.
    run_dir: Path
    geometry: tuple[int, int] = (1920, 1080)
    headless: bool = False
    env: dict[str, str] = field(default_factory=dict)


def resolve_display_mode(requested: str | None, headless: Any, vnc_default: bool) -> str:
    """Where a launch draws: ``headless``, ``virtual`` (own Xvnc) or ``window``.

    An explicit mode wins. Otherwise a headless launch stays headless and a
    headed one follows ``PPM_VNC``, which is only the default since a single
    instance can serve all three.
    """
    if requested is not None:
        if requested not in DISPLAY_MODES:
            raise BrowserLaunchError(
                f"Unknown display mode {requested!r}; use one of {', '.join(DISPLAY_MODES)}"
            )
        return requested
    if headless:
        return "headless"
    return "virtual" if vnc_default else "window"


def window_env(environ: dict[str, str]) -> dict[str, str]:
    """The environment that puts a headed browser on the desktop session.

    Wayland when its socket is reachable (in Docker, the mounted compositor
    socket), else X11. Wayland needs no xhost or cookie, so it is preferred.
    """
    env = dict(environ)
    wayland = env.get("WAYLAND_DISPLAY", "")
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if wayland and (os.path.isabs(wayland) or os.path.exists(os.path.join(runtime, wayland))):
        return env
    env.pop("WAYLAND_DISPLAY", None)
    if env.get("DISPLAY"):
        return env
    raise BrowserLaunchError(
        "No desktop session to open a window on: WAYLAND_DISPLAY and DISPLAY are unset. "
        "In Docker, add docker-compose.desktop.yml, or launch with mode 'virtual' or 'headless'."
    )


def available_modes(vnc_default: bool) -> dict[str, Any]:
    """Which display modes this instance can launch in, and which a plain launch uses.

    For the web UI's launch menu: a mode that cannot work is shown disabled with
    the reason, rather than failing after the click.
    """
    linux = sys.platform.startswith("linux")
    reasons: dict[str, str | None] = {"headless": None}
    reasons["virtual"] = (
        None
        if linux and shutil.which("Xvnc")
        else "Needs TigerVNC's Xvnc (in the Docker image; on Linux, install tigervnc)"
        if linux
        else "Only on Linux"
    )
    try:
        if linux:
            window_env(dict(os.environ))
        reasons["window"] = None
    except BrowserLaunchError:
        reasons["window"] = "No desktop here. In Docker, add docker-compose.desktop.yml"
    return {
        "default": resolve_display_mode(None, False, vnc_default),
        "modes": [
            {"mode": mode, "available": reasons[mode] is None, "reason": reasons[mode]}
            for mode in DISPLAY_MODES
        ],
    }


def display_args(mode: str, environ: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The switches and environment that put the browser where ``mode`` says."""
    if mode == "headless":
        return ["--headless"], environ
    if mode == "virtual":
        # Chrome prefers Wayland whenever it can reach a compositor, which would
        # put the window there instead of on the VNC display.
        env = {k: v for k, v in environ.items() if k != "WAYLAND_DISPLAY"}
        return ["--ozone-platform=x11"], env
    return [], environ


def _read_devtools_port(user_data_dir: Path) -> tuple[int, str] | None:
    """The CDP port and browser target path Chrome wrote, once it has."""
    try:
        lines = (user_data_dir / "DevToolsActivePort").read_text().splitlines()
        return int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None


def reap_orphan(run_dir: Path) -> bool:
    """Kill the browser a dead manager left behind in ``run_dir``; remove the dir.

    The browser runs in a session of its own, so it outlives a manager that was
    killed. Left running it would hold the profile directory (Chrome's
    SingletonLock hands a second launch over to it) with nobody watching it.
    Only a process whose command line names this run directory is killed, so a
    reused pid is never mistaken for it. Returns whether a browser was killed.
    """
    import psutil

    killed = False
    try:
        pid = int((run_dir / PID_FILE).read_text().strip())
        process = psutil.Process(pid)
        if any(str(run_dir) in arg for arg in process.cmdline()):
            os.killpg(pid, signal.SIGKILL)
            killed = True
    except (OSError, ValueError, psutil.Error):
        pass
    shutil.rmtree(run_dir, ignore_errors=True)
    return killed


def _log_tail(path: Path, limit: int = 800) -> str:
    try:
        return path.read_text(errors="replace").strip()[-limit:]
    except OSError:
        return ""


class BrowserSession:
    """A single running Phasmid browser tied to a profile."""

    def __init__(
        self,
        profile_id: str,
        process: asyncio.subprocess.Process,
        cdp_port: int,
        cdp_path: str,
        run_dir: Path | None = None,
        vnc: VncDisplays | None = None,
        mode: str = "window",
    ):
        self.profile_id = profile_id
        self.process = process
        self.process_id: int | None = process.pid
        self.cdp_port = cdp_port
        self.cdp_path = cdp_path
        self.run_dir = run_dir
        self.started_at = datetime.now()
        self.monitor_task: asyncio.Task[None] | None = None
        self.on_exit: ExitHandler | None = None
        # The displays registry, when this browser draws to its own VNC display.
        self.vnc = vnc
        self.mode = mode
        self._terminated = False

    @property
    def cdp_endpoint(self) -> str:
        """The browser's own loopback CDP WebSocket; reached through the API relay."""
        return f"ws://127.0.0.1:{self.cdp_port}{self.cdp_path}"

    async def _close_over_cdp(self) -> None:
        """Ask the browser to close itself, so it writes its session and cookies out."""
        from websockets.asyncio.client import connect

        async with connect(self.cdp_endpoint, open_timeout=3, max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.recv(), timeout=3)

    def _signal_group(self, sig: int) -> None:
        if self.process.returncode is not None or self.process_id is None:
            return
        try:
            os.killpg(self.process_id, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            with contextlib.suppress(ProcessLookupError):
                self.process.send_signal(sig)

    async def terminate(self) -> None:
        """Close the browser and stop its monitor task. Safe to call twice."""
        if self._terminated:
            return
        self._terminated = True
        logger.info(f"Terminating browser session for profile {self.profile_id}")

        # The monitor is the usual caller here (_monitor -> _handle_exit ->
        # terminate). Cancelling the current task would abort the cleanup below
        # at its next await, so only a monitor that is someone else is stopped.
        monitor = self.monitor_task
        if monitor and not monitor.done() and asyncio.current_task() is not monitor:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

        if self.process.returncode is None:
            try:
                await self._close_over_cdp()
                await asyncio.wait_for(self.process.wait(), timeout=CLOSE_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - fall through to signals
                logger.debug(f"Graceful close of {self.profile_id} failed: {exc}")
        if self.process.returncode is None:
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=TERM_TIMEOUT)
            except TimeoutError:
                self._signal_group(signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.process.wait(), timeout=5)
        # Children of the browser (renderers, the GPU process) share its group;
        # one that outlived its parent must not keep the profile directory busy.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if self.process_id is not None:
                os.killpg(self.process_id, signal.SIGKILL)

        # The browser has to be gone before its display is pulled away.
        if self.vnc is not None:
            try:
                await self.vnc.stop(self.profile_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error stopping VNC display for {self.profile_id}: {exc}")

        # The run directory holds proxy credentials; it must not outlive the session.
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)

    def info(self) -> dict[str, Any]:
        """Return a serializable summary of this session."""
        return {
            "profile_id": self.profile_id,
            "process_id": self.process_id,
            "started_at": self.started_at.isoformat(),
            "vnc": self.vnc is not None and self.vnc.get(self.profile_id) is not None,
            "mode": self.mode,
            "cdp": True,
        }


class BrowserSessionManager:
    """Track and control the browsers currently running."""

    def __init__(
        self,
        storage: StorageManager | None = None,
        holder: str | None = None,
        vnc: VncDisplays | None = None,
    ) -> None:
        self.active_sessions: dict[str, BrowserSession] = {}
        # Off unless a caller asks for it, so tests and the CLI never spawn Xvnc.
        self.vnc = vnc or VncDisplays(enabled=False)
        # Profile id -> how many launches are currently starting a browser. A
        # count rather than a set, because two concurrent launches of one
        # profile both mark it and the first to leave would otherwise clear the
        # mark while the other is still starting.
        self._starting: dict[str, int] = {}
        # The event loop only holds weak references to tasks, so a teardown
        # suspended mid-close could be garbage-collected and take the cleanup
        # with it. Hold a strong reference until done.
        self._exit_tasks: set[asyncio.Task[None]] = set()
        # Lease bookkeeping. Without a storage and a holder id (unit tests, and
        # anything that only watches processes) every lease call below is a
        # no-op.
        self._storage = storage
        self._holder = holder
        self._heartbeat_task: asyncio.Task[None] | None = None

    def start_heartbeat(self, interval: float = 30.0) -> None:
        """Begin renewing this process's leases in the background.

        The heartbeat is the difference between "this profile is running" and
        "this profile was running when someone last crashed": while it beats
        the lease stays alive, and when it stops — SIGKILL, a power cut — the
        lease outlives the browser by at most its TTL and then frees itself.
        """
        if self._storage is None or self._holder is None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    async def stop_heartbeat(self) -> None:
        """Stop the renewal loop, and wait for the beat in flight to finish."""
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._heartbeat_task
        self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self._renew_leases()
            except Exception as exc:  # noqa: BLE001 - one bad beat must not stop the loop
                logger.warning(f"Lease heartbeat failed: {exc}")

    async def _renew_leases(self) -> None:
        """Renew every lease we hold, and close any browser whose lease is gone."""
        if self._storage is None or self._holder is None or not self.active_sessions:
            return
        profile_ids = list(self.active_sessions)
        # The configured TTL, not a constant: a hardcoded value here would
        # shorten a deliberately long lease on every beat and let it expire
        # under a browser that is still running.
        ttl_seconds = get_settings().lease_ttl
        renewed = await self._storage.renew_lease(profile_ids, self._holder, ttl_seconds)
        if renewed == len(profile_ids):
            return
        # Something was taken from us. The bulk update already renewed whatever
        # survived; ask one at a time only to learn which ones those were.
        for profile_id in profile_ids:
            if await self._storage.renew_lease([profile_id], self._holder, ttl_seconds) > 0:
                continue
            logger.warning(
                f"Lost the lease on profile {profile_id} — another instance may be "
                "driving this identity; closing the browser."
            )
            # No attempt to take it back: whoever holds it now is already
            # running the profile, and racing them is the corruption itself.
            await self.close(profile_id)

    def is_running(self, profile_id: str) -> bool:
        """Return whether a browser is currently tracked for the profile."""
        return profile_id in self.active_sessions

    def is_live(self, profile_id: str) -> bool:
        """Whether a browser is running *or* still starting for the profile.

        ``is_running`` is false while the browser is starting, so a lease
        released on that answer could be taken away from a browser that is
        coming up. Lease decisions use this; the user-facing "is it open"
        answers stay on ``is_running``.
        """
        return profile_id in self.active_sessions or profile_id in self._starting

    def list_active(self) -> list[dict[str, Any]]:
        """Return summaries of the active sessions. A pure read."""
        return [session.info() for session in self.active_sessions.values()]

    async def launch(
        self,
        profile_id: str,
        plan: LaunchPlan,
        on_exit: ExitHandler | None = None,
        mode: str | None = None,
    ) -> BrowserSession:
        """Start a Phasmid browser and register a monitored session.

        ``mode`` picks the display per launch (see :func:`resolve_display_mode`).
        """
        if profile_id in self.active_sessions:
            return self.active_sessions[profile_id]

        window_requested = mode == "window"
        mode = resolve_display_mode(mode, plan.headless, self.vnc.enabled)
        use_vnc = mode == "virtual"
        env = {**os.environ, **plan.env}
        # Asked for by name, a window has to have somewhere to go; by default the
        # browser inherits this process's display untouched.
        if window_requested and sys.platform.startswith("linux"):
            env = window_env(env)
        switches, env = display_args(mode, env)

        # Counted before the first await: from here until the session is
        # registered, is_live() says this profile is coming up, and nothing
        # may hand its lease away.
        self._starting[profile_id] = self._starting.get(profile_id, 0) + 1
        try:
            process: asyncio.subprocess.Process | None = None
            log_path = plan.run_dir / "browser.log"
            try:
                if use_vnc:
                    display = await self.vnc.start(profile_id, *plan.geometry)
                    env["DISPLAY"] = display.name
                (plan.user_data_dir / "DevToolsActivePort").unlink(missing_ok=True)
                with log_path.open("wb") as log:
                    process = await asyncio.create_subprocess_exec(
                        *plan.argv,
                        *switches,
                        env=env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                # For the next start of this app, should this one die without
                # closing the browser: see ProfileManager.reap_orphans.
                (plan.run_dir / PID_FILE).write_text(str(process.pid))
                port, path = await self._wait_for_devtools(process, plan.user_data_dir, log_path)
            except (Exception, asyncio.CancelledError) as exc:
                if process is not None and process.returncode is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(process.wait(), timeout=5)
                if use_vnc:
                    await self.vnc.stop(profile_id)
                # It holds the proxy credentials, and no session will clean it up.
                shutil.rmtree(plan.run_dir, ignore_errors=True)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, BrowserLaunchError):
                    raise
                raise BrowserLaunchError(f"Failed to launch browser: {exc}") from exc

            session = BrowserSession(
                profile_id,
                process,
                port,
                path,
                plan.run_dir,
                self.vnc if use_vnc else None,
                mode,
            )
            session.on_exit = on_exit
            self.active_sessions[profile_id] = session
        finally:
            remaining = self._starting.get(profile_id, 1) - 1
            if remaining > 0:
                self._starting[profile_id] = remaining
            else:
                self._starting.pop(profile_id, None)

        session.monitor_task = asyncio.create_task(self._monitor(profile_id, process))
        logger.info(
            f"Browser launched for profile {profile_id} (pid={process.pid}, mode={mode}, "
            f"cdp port {port})"
        )
        return session

    async def _wait_for_devtools(
        self, process: asyncio.subprocess.Process, user_data_dir: Path, log_path: Path
    ) -> tuple[int, str]:
        """Wait until Chrome has written its CDP port, or report why it exited."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STARTUP_TIMEOUT
        while loop.time() < deadline:
            found = _read_devtools_port(user_data_dir)
            if found is not None:
                return found
            if process.returncode is not None:
                break
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=0.2)
        if process.returncode is not None:
            tail = _log_tail(log_path)
            raise BrowserLaunchError(
                f"The browser exited during startup (code {process.returncode})"
                + (f": {tail}" if tail else "")
            )
        raise BrowserLaunchError(
            f"The browser did not open its debugging port within {STARTUP_TIMEOUT:.0f}s"
        )

    async def _handle_exit(self, profile_id: str) -> None:
        """Tear down a session exactly once and notify the exit handler."""
        session = self.active_sessions.pop(profile_id, None)
        if session is None:
            return
        await session.terminate()
        # The browser is gone, so the lease must not outlive it. release_lease
        # is guarded on the holder id, so a lease already taken over by another
        # instance is left exactly where it is.
        await self._release_lease(profile_id)
        if session.on_exit is not None:
            try:
                await session.on_exit(profile_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"on_exit handler failed for {profile_id}: {exc}")

    async def close(self, profile_id: str) -> bool:
        """Close a single browser. Returns ``False`` if it was not running."""
        session = self.active_sessions.pop(profile_id, None)
        if session is None:
            return False
        await session.terminate()
        return True

    async def close_and_release(self, profile_id: str) -> bool:
        """Close a browser and hand its lease back, if we still hold it.

        Separate from :meth:`close` because one caller must not release: when
        the heartbeat finds a lease gone, the browser still has to be closed,
        and clearing the lease then would clear the *new* holder's.
        """
        closed = await self.close(profile_id)
        if closed:
            await self._release_lease(profile_id)
        return closed

    async def _release_lease(self, profile_id: str) -> None:
        """Best-effort lease release. Teardown must not fail on bookkeeping."""
        if self._storage is None or self._holder is None:
            return
        try:
            await self._storage.release_lease(profile_id, self._holder)
        except Exception as exc:  # noqa: BLE001 - a stuck release must not block cleanup
            logger.warning(f"Could not release the lease on {profile_id}: {exc}")

    async def close_all(self) -> int:
        """Close every active browser and return how many were closed."""
        count = 0
        for profile_id in list(self.active_sessions.keys()):
            if await self.close_and_release(profile_id):
                count += 1
        return count

    async def _monitor(self, profile_id: str, process: asyncio.subprocess.Process) -> None:
        """Clean up when the browser process exits, whoever ended it."""
        code = await process.wait()
        logger.info(f"Browser for profile {profile_id} exited (code {code})")
        await self._handle_exit(profile_id)
