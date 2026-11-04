"""Browser session lifecycle: launch, monitor, and close Camoufox instances.

Extracted from ``profile_manager`` so profile CRUD and browser control have
clear, independently testable responsibilities.

Cleanup is driven primarily by Playwright's ``close``/``disconnected`` events, so
a user closing the browser window is detected and the session is torn down. OS
process polling is only a best-effort fallback: with a persistent context the
resolvable pid is Playwright's driver process, not Firefox, so it is not a
reliable window-close signal on its own.
"""

import asyncio
import os
import shutil
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psutil
from loguru import logger

from ..config import get_settings
from .vnc import VncDisplays, display_geometry

if TYPE_CHECKING:
    # Type-only: importing StorageManager at runtime would be a cycle, since
    # database.py has no need of this module at all.
    from .database import StorageManager

try:
    from camoufox.async_api import AsyncCamoufox

    CAMOUFOX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without camoufox installed
    AsyncCamoufox = None  # type: ignore[assignment, misc]
    CAMOUFOX_AVAILABLE = False

ExitHandler = Callable[[str], Awaitable[None]]


class BrowserLaunchError(RuntimeError):
    """Raised when a Camoufox browser fails to launch."""


# Where a headed browser draws is chosen per launch, not per instance.
DISPLAY_MODES = ("headless", "virtual", "window")


def _resolve_process_id(obj: Any) -> int | None:
    """Best-effort resolution of a driver/browser OS process id.

    Playwright does not expose this publicly, so this walks known internal
    attributes and returns ``None`` if none are available. It never fabricates a
    placeholder pid. Note: for a persistent context this is the Playwright driver
    process, used only as a forceful-kill fallback.
    """
    candidates = (
        lambda b: b._browser_process.pid,  # noqa: SLF001
        lambda b: b.browser._impl._connection._transport._proc.pid,  # noqa: SLF001
        lambda b: b._impl._connection._transport._proc.pid,  # noqa: SLF001
    )
    for getter in candidates:
        try:
            pid = getter(obj)
            if pid:
                return int(pid)
        except Exception:  # noqa: BLE001 - private attributes vary across versions
            continue
    return None


def resolve_display_mode(requested: str | None, headless: Any, vnc_default: bool) -> str:
    """Where a launch draws: ``headless``, ``virtual`` (own Xvnc) or ``window``.

    An explicit mode wins. Otherwise a headless launch stays headless and a
    headed one follows ``PPM_VNC``, which is only the default since a single
    instance can now serve all three.
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


async def _bind_playwright(browser: Any, profile_id: str) -> str | None:
    """Serve the browser to outside Playwright clients; returns the ws endpoint.

    Loopback only and unauthenticated, so it is never handed out: clients come
    in through the authenticated relay in ``api/routes/automation.py``. A client
    calling ``browser.close()`` only disconnects; the profile keeps running.
    """
    # A persistent profile launches as a BrowserContext; bind lives on its Browser.
    target = browser.browser if hasattr(browser, "pages") else browser
    try:
        result = await target.bind(f"phasmid-pm {profile_id}", host="127.0.0.1", port=0)
        return result["endpoint"]
    except Exception as exc:  # noqa: BLE001 - the browser is still usable by hand
        logger.warning(f"Could not expose {profile_id} to Playwright clients: {exc}")
        return None


class BrowserSession:
    """A single running Camoufox browser tied to a profile."""

    def __init__(
        self,
        profile_id: str,
        camoufox: Any,
        process_id: int | None = None,
        vnc: VncDisplays | None = None,
        mode: str = "window",
    ):
        self.profile_id = profile_id
        self.camoufox = camoufox  # AsyncCamoufox context manager instance
        self.process_id = process_id
        self.started_at = datetime.now()
        self.monitor_task: asyncio.Task | None = None
        self.on_exit: ExitHandler | None = None
        # The displays registry, when this browser draws to its own VNC display.
        self.vnc = vnc
        self.mode = mode
        # Loopback Playwright server for this browser; reached through the relay.
        self.playwright_endpoint: str | None = None
        self._terminated = False

    async def terminate(self) -> None:
        """Close the browser and stop its monitor task. Safe to call twice."""
        if self._terminated:
            return
        self._terminated = True
        logger.info(f"Terminating browser session for profile {self.profile_id}")

        # The monitor can be the caller here (_monitor -> _handle_exit -> terminate),
        # and a task cannot await itself; cancelling is enough in that case.
        monitor = self.monitor_task
        if monitor and not monitor.done():
            monitor.cancel()
            if asyncio.current_task() is not monitor:
                try:
                    await monitor
                except asyncio.CancelledError:
                    pass

        if self.camoufox is not None:
            try:
                await self.camoufox.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error closing browser for {self.profile_id}: {exc}")

        # Best-effort: make sure the driver process is gone.
        if self.process_id:
            try:
                process = psutil.Process(self.process_id)
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error killing process {self.process_id}: {exc}")

        # Last: the browser has to be gone before its display is pulled away.
        if self.vnc is not None:
            try:
                await self.vnc.stop(self.profile_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error stopping VNC display for {self.profile_id}: {exc}")

    def info(self) -> dict[str, Any]:
        """Return a serializable summary of this session."""
        return {
            "profile_id": self.profile_id,
            "process_id": self.process_id,
            "started_at": self.started_at.isoformat(),
            "vnc": self.vnc is not None and self.vnc.get(self.profile_id) is not None,
            "mode": self.mode,
            "playwright": self.playwright_endpoint is not None,
        }


class BrowserSessionManager:
    """Track and control the browsers currently running."""

    def __init__(
        self,
        storage: "StorageManager | None" = None,
        holder: str | None = None,
        vnc: VncDisplays | None = None,
    ) -> None:
        self.active_sessions: dict[str, BrowserSession] = {}
        # Off unless a caller asks for it, so tests and the CLI never spawn Xvnc.
        self.vnc = vnc or VncDisplays(enabled=False)
        # Profile id -> how many launches are currently inside camoufox.start().
        # A count rather than a set, because two concurrent launches of one
        # profile both mark it and the first to leave would otherwise clear the
        # mark while the other is still starting.
        self._starting: dict[str, int] = {}
        # The event loop only holds weak references to tasks, so a teardown
        # suspended inside camoufox.__aexit__ could be garbage-collected and take
        # the primary cleanup path with it. Hold a strong reference until done.
        self._exit_tasks: set[asyncio.Task[None]] = set()
        # Lease bookkeeping. Without a storage and a holder id (unit tests, and
        # anything that only watches processes) every lease call below is a
        # no-op and this class behaves exactly as it did before leases existed.
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
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
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

        ``is_running`` is false for the whole time ``camoufox.start()`` is
        being awaited, so a lease released on that answer can be taken away
        from a browser that is coming up. Lease decisions use this; the
        user-facing "is it open" answers stay on ``is_running``.
        """
        return profile_id in self.active_sessions or profile_id in self._starting

    def list_active(self) -> list[dict[str, Any]]:
        """Return summaries of the active sessions.

        A pure read: it used to drop sessions whose driver process had gone,
        which skipped ``terminate()`` and the exit handler and so leaked the
        Camoufox context. Teardown belongs to the close event and, failing that,
        to :meth:`_monitor`; both route through ``_handle_exit``.
        """
        return [session.info() for session in self.active_sessions.values()]

    async def launch(
        self,
        profile_id: str,
        launch_options: dict[str, Any],
        on_exit: ExitHandler | None = None,
        mode: str | None = None,
    ) -> BrowserSession:
        """Launch a Camoufox browser and register a monitored session.

        ``mode`` picks the display per launch (see :func:`resolve_display_mode`).
        """
        if not CAMOUFOX_AVAILABLE:
            raise BrowserLaunchError(
                "Camoufox is not installed. Install it with: pip install 'camoufox[geoip]'"
            )
        if profile_id in self.active_sessions:
            return self.active_sessions[profile_id]

        window_requested = mode == "window"
        mode = resolve_display_mode(mode, launch_options.get("headless"), self.vnc.enabled)
        use_vnc = mode == "virtual"
        # A copy: the caller's options are echoed back in the API response.
        launch_options = {
            **launch_options,
            # Kept when truthy, so Camoufox's own headless="virtual" still passes through.
            "headless": (launch_options.get("headless") or True) if mode == "headless" else False,
        }
        base_env = {**os.environ, **(launch_options.get("env") or {})}
        # Asked for by name, a window has to have somewhere to go; by default the
        # browser inherits this process's display untouched, as it always has.
        # Only Linux picks a display through the environment.
        if window_requested and sys.platform.startswith("linux"):
            launch_options["env"] = window_env(base_env)

        # Counted before the first await: from here until the session is
        # registered, is_live() says this profile is coming up, and nothing
        # may hand its lease away.
        self._starting[profile_id] = self._starting.get(profile_id, 0) + 1
        try:
            try:
                if use_vnc:
                    display = await self.vnc.start(profile_id, *display_geometry(launch_options))
                    # Firefox prefers Wayland whenever it can reach a compositor,
                    # which would put the window there instead of on this display.
                    base_env.pop("WAYLAND_DISPLAY", None)
                    launch_options["env"] = {**base_env, "DISPLAY": display.name}
                camoufox = AsyncCamoufox(**launch_options)
                browser = await camoufox.start()
            except (Exception, asyncio.CancelledError) as exc:
                if use_vnc:
                    await self.vnc.stop(profile_id)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BrowserLaunchError(f"Failed to launch browser: {exc}") from exc

            process_id = _resolve_process_id(browser) or _resolve_process_id(camoufox)
            session = BrowserSession(
                profile_id, camoufox, process_id, self.vnc if use_vnc else None, mode
            )
            session.on_exit = on_exit
            self.active_sessions[profile_id] = session
        finally:
            remaining = self._starting.get(profile_id, 1) - 1
            if remaining > 0:
                self._starting[profile_id] = remaining
            else:
                self._starting.pop(profile_id, None)

        # Primary signal: the browser/context closing (e.g. the user closes the window).
        self._register_close_handler(browser, profile_id)
        # Fallback: poll the driver pid, only if we could resolve one.
        if process_id:
            session.monitor_task = asyncio.create_task(self._monitor(profile_id, process_id))
        # After registration, so a cancellation here leaves a tracked session
        # rather than an orphaned browser.
        session.playwright_endpoint = await _bind_playwright(browser, profile_id)

        logger.info(f"Browser launched for profile {profile_id} (pid={process_id}, mode={mode})")
        return session

    def _register_close_handler(self, browser: Any, profile_id: str) -> None:
        """Wire Playwright close/disconnect events to session cleanup."""
        loop = asyncio.get_running_loop()

        def _on_close(*_: Any) -> None:
            task = loop.create_task(self._handle_exit(profile_id))
            self._exit_tasks.add(task)
            task.add_done_callback(self._exit_tasks.discard)

        for event in ("close", "disconnected"):
            try:
                browser.on(event, _on_close)
            except Exception:  # noqa: BLE001 - Browser vs BrowserContext expose different events
                continue

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

    async def _monitor(self, profile_id: str, process_id: int) -> None:
        """Fallback watchdog: clean up if the driver process disappears."""
        while psutil.pid_exists(process_id):
            await asyncio.sleep(10)
        await self._handle_exit(profile_id)
