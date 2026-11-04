"""Per-profile VNC displays, so a browser running in a container can be seen.

With ``PPM_VNC`` on, every headed launch gets its own TigerVNC ``Xvnc`` X server.
The browser draws to that display and nothing else, so one viewer never shows
another profile's windows. Xvnc listens on loopback only and has no password of
its own: the only way in is the authenticated WebSocket route in
``api/routes/viewer.py``, which relays the RFB stream to noVNC in the web UI.
"""

import asyncio
import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from loguru import logger

# Display numbers start well above :0/:1 so they cannot collide with an X server
# that is already there; the RFB port follows the VNC convention of 5900 + n.
BASE_DISPLAY = 100
RFB_BASE_PORT = 5900
DEFAULT_GEOMETRY = (1920, 1080)


class VncError(RuntimeError):
    """Raised when a VNC display cannot be started."""


@dataclass
class VncDisplay:
    number: int
    width: int
    height: int
    process: subprocess.Popen[bytes]

    @property
    def name(self) -> str:
        return f":{self.number}"

    @property
    def rfb_port(self) -> int:
        return RFB_BASE_PORT + self.number


def display_geometry(launch_options: dict[str, Any]) -> tuple[int, int]:
    """The size of the browser window, so the window fills the whole display.

    There is no window manager to maximize it, and Camoufox opens the window at
    the fingerprint's outer size. A display sized to the fingerprint's screen
    instead would show the window in one corner of a mostly black frame. Pages
    see the spoofed screen either way, so the real display size does not leak.
    """
    config = launch_options.get("config") or {}
    for width_key, height_key in (
        ("window.outerWidth", "window.outerHeight"),
        ("screen.width", "screen.height"),
    ):
        width, height = config.get(width_key), config.get(height_key)
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
    window = launch_options.get("window")
    if window:
        return int(window[0]), int(window[1])
    return DEFAULT_GEOMETRY


def _clear_stale_lock(number: int) -> None:
    """Remove the lock of a display whose X server is gone.

    /tmp outlives a container restart, and Xvnc refuses a display whose lock file
    is still there, so a crash would otherwise make that display unusable.
    """
    lock = Path(f"/tmp/.X{number}-lock")
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None and psutil.pid_exists(pid):
        return
    lock.unlink(missing_ok=True)
    Path(f"/tmp/.X11-unix/X{number}").unlink(missing_ok=True)


def _log_path(number: int) -> Path:
    return Path(f"/tmp/xvnc-{number}.log")


class VncDisplays:
    """Allocate, start and stop the VNC displays of running profiles."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._displays: dict[str, VncDisplay] = {}
        self._reserved: set[int] = set()
        self._lock = asyncio.Lock()

    def get(self, profile_id: str) -> VncDisplay | None:
        return self._displays.get(profile_id)

    async def start(self, profile_id: str, width: int, height: int) -> VncDisplay:
        """Start an Xvnc for the profile and wait until it accepts connections."""
        binary = shutil.which("Xvnc")
        if binary is None:
            raise VncError("PPM_VNC is on but Xvnc is not installed (tigervnc-standalone-server)")

        async with self._lock:
            number = BASE_DISPLAY
            while number in self._reserved:
                number += 1
            self._reserved.add(number)

        process: subprocess.Popen[bytes] | None = None
        try:
            _clear_stale_lock(number)
            # A file, not a pipe: Xvnc logs every client connection, and a pipe
            # nobody drains would eventually fill and freeze the display.
            with open(_log_path(number), "wb") as log:
                process = subprocess.Popen(
                    [
                        binary,
                        f":{number}",
                        "-geometry",
                        f"{width}x{height}",
                        "-depth",
                        "24",
                        "-rfbport",
                        str(RFB_BASE_PORT + number),
                        # Loopback only, no VNC password: the WebSocket relay is the
                        # only client, and it does the authentication.
                        "-localhost",
                        "-SecurityTypes",
                        "None",
                        "-AlwaysShared",
                        "-nolisten",
                        "tcp",
                    ],
                    stdout=log,
                    stderr=log,
                )
            display = VncDisplay(number, width, height, process)
            await self._wait_ready(display)
        except BaseException:
            if process is not None:
                with contextlib.suppress(Exception):
                    process.kill()
            async with self._lock:
                self._reserved.discard(number)
            raise

        self._displays[profile_id] = display
        logger.info(f"VNC display {display.name} ({width}x{height}) started for {profile_id}")
        return display

    async def _wait_ready(self, display: VncDisplay, timeout: float = 10.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if display.process.poll() is not None:
                try:
                    output = _log_path(display.number).read_text(errors="replace")
                except OSError:
                    output = ""
                raise VncError(f"Xvnc {display.name} exited: {output.strip()[-500:]}")
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", display.rfb_port)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        raise VncError(f"Xvnc {display.name} did not start within {timeout:.0f}s")

    async def stop(self, profile_id: str) -> None:
        """Stop the profile's display, if it has one. Safe to call twice."""
        display = self._displays.pop(profile_id, None)
        if display is None:
            return
        display.process.terminate()
        try:
            await asyncio.to_thread(display.process.wait, 5)
        except subprocess.TimeoutExpired:
            display.process.kill()
        async with self._lock:
            self._reserved.discard(display.number)
        logger.info(f"VNC display {display.name} stopped for {profile_id}")
