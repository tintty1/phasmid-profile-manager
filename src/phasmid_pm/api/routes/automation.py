"""WebSocket relay from an outside Playwright client to a profile's browser.

Every launched browser serves Playwright on a loopback port with no auth of its
own (``browser.bind``, see ``core/browser_session.py``). This route is the way
in: it applies the API's auth rules, then passes Playwright's protocol messages
through untouched. Agents connect with::

    browser = await playwright.firefox.connect(
        "ws://127.0.0.1:8000/api/v1/profiles/<id>/playwright",
        headers={"x-api-key": "..."},  # only when PPM_API_KEY is set
    )
    page = browser.contexts[0].pages[0]

One port for everything means it works unchanged from Docker. A client's
``browser.close()`` only ends its connection; the profile keeps running.
"""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from phasmid_pm.api.dependencies import credentials_ok, get_profile_manager
from phasmid_pm.api.routes.viewer import _api_key_from_protocols, _origin_allowed
from phasmid_pm.core import auth

router = APIRouter()

# For uvicorn's ws_max_size, whose 16 MiB default is below what Playwright sends
# in one message: set_input_files inlines files up to 50 MB as base64.
WS_MAX_SIZE = 100 * 1024 * 1024


@router.websocket("/profiles/{profile_id}/playwright")
async def playwright_relay(websocket: WebSocket, profile_id: str) -> None:
    """Relay one Playwright client to a profile's browser, until either side leaves."""
    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    protocols = list(websocket.scope.get("subprotocols") or [])
    api_key = websocket.headers.get("x-api-key") or _api_key_from_protocols(protocols)
    if not await credentials_ok(websocket.cookies.get(auth.SESSION_COOKIE), api_key):
        await websocket.close(code=4401, reason="Authentication required")
        return

    session = get_profile_manager().browser_sessions.active_sessions.get(profile_id)
    endpoint = session.playwright_endpoint if session is not None else None
    if endpoint is None:
        await websocket.close(code=4404, reason="This profile has no browser to drive")
        return

    try:
        # No size cap and no pings: Playwright messages carry whole screenshots,
        # and the client runs its own keepalive across the relay.
        upstream = await connect(endpoint, max_size=None, ping_interval=None, open_timeout=10)
    except (OSError, TimeoutError, ConnectionClosed) as exc:
        logger.warning(f"Playwright server of {profile_id} refused the relay: {exc}")
        await websocket.close(code=1011, reason="Browser unavailable")
        return

    await websocket.accept()

    async def client_to_browser() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            await upstream.send(text if text is not None else message.get("bytes") or b"")

    async def browser_to_client() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    tasks = [
        asyncio.create_task(client_to_browser()),
        asyncio.create_task(browser_to_client()),
    ]
    try:
        # Whichever side ends first ends the relay.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(
                asyncio.CancelledError, WebSocketDisconnect, ConnectionClosed, OSError, RuntimeError
            ):
                await task
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()
