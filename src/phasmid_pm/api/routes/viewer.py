"""WebSocket relay from the web UI's noVNC viewer to a profile's VNC display.

The display's Xvnc listens on loopback with no password, so this route is the
whole access control: it applies the API's own auth rules, refuses cross-site
pages, and then copies bytes both ways without looking at them.
"""

import asyncio
import base64
import binascii
import contextlib
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from phasmid_pm.api.dependencies import credentials_ok, get_profile_manager
from phasmid_pm.config import get_settings
from phasmid_pm.core import auth

router = APIRouter()

# A browser cannot set headers on a WebSocket, and a key in the URL would land in
# access logs, so the UI offers its API key as a subprotocol: "ppm-key.<base64url>".
KEY_PROTOCOL_PREFIX = "ppm-key."


def _api_key_from_protocols(protocols: list[str]) -> str | None:
    for protocol in protocols:
        if protocol.startswith(KEY_PROTOCOL_PREFIX):
            encoded = protocol[len(KEY_PROTOCOL_PREFIX) :]
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                return None
    return None


def _origin_allowed(websocket: WebSocket) -> bool:
    """Refuse pages from other sites (cross-site WebSocket hijacking).

    The session cookie rides along on any WebSocket the browser opens to this
    host, whichever page opened it, so the Origin is what stops another site
    from watching and driving a profile. Clients that send no Origin (scripts)
    have no cookie to borrow and still have to authenticate.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if origin in get_settings().cors_origins:
        return True
    return urlsplit(origin).netloc == websocket.headers.get("host")


@router.websocket("/profiles/{profile_id}/vnc")
async def vnc_relay(websocket: WebSocket, profile_id: str) -> None:
    """Relay a profile's VNC display to the viewer, for as long as both stay open."""
    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    protocols = list(websocket.scope.get("subprotocols") or [])
    api_key = websocket.headers.get("x-api-key") or _api_key_from_protocols(protocols)
    if not await credentials_ok(websocket.cookies.get(auth.SESSION_COOKIE), api_key):
        await websocket.close(code=4401, reason="Authentication required")
        return

    display = get_profile_manager().browser_sessions.vnc.get(profile_id)
    if display is None:
        await websocket.close(code=4404, reason="This profile has no VNC display running")
        return

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", display.rfb_port)
    except OSError as exc:
        logger.warning(f"VNC display {display.name} refused the relay: {exc}")
        await websocket.close(code=1011, reason="VNC display unavailable")
        return

    # Echo noVNC's "binary" protocol, never the key one.
    await websocket.accept(subprotocol="binary" if "binary" in protocols else None)

    async def viewer_to_display() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes") or (message.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def display_to_viewer() -> None:
        while chunk := await reader.read(65536):
            await websocket.send_bytes(chunk)

    tasks = [
        asyncio.create_task(viewer_to_display()),
        asyncio.create_task(display_to_viewer()),
    ]
    try:
        # Whichever side ends first ends the relay.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, OSError, RuntimeError
            ):
                await task
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await websocket.close()
