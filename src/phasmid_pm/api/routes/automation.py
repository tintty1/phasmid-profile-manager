"""WebSocket relay from an outside CDP client to a profile's browser.

Every launched browser listens for CDP on a loopback port with no auth of its
own (``--remote-debugging-port=0``, see ``core/browser_session.py``). This route
is the way in: it applies the API's auth rules, then passes CDP messages through
untouched. Agents connect with::

    browser = await playwright.chromium.connect_over_cdp(
        "ws://127.0.0.1:8000/api/v1/profiles/<id>/cdp",
        headers={"x-api-key": "..."},  # only when PPM_API_KEY is set
    )
    page = browser.contexts[0].pages[0]

or ``puppeteer.connect({browserWSEndpoint})``. One port for everything means it
works unchanged from Docker. Phasmid keeps an attached client invisible to pages
(``automation/cdp-stealth.patch``). Playwright's ``browser.close()`` on a CDP
connection only disconnects, and the profile keeps running; close it with
``POST /profiles/<id>/close`` (or CDP ``Browser.close``).
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

# For uvicorn's ws_max_size, whose 16 MiB default is below what a CDP client
# sends in one message: Playwright's set_input_files inlines files as base64.
WS_MAX_SIZE = 100 * 1024 * 1024


@router.websocket("/profiles/{profile_id}/cdp")
async def cdp_relay(websocket: WebSocket, profile_id: str) -> None:
    """Relay one CDP client to a profile's browser, until either side leaves."""
    if not _origin_allowed(websocket):
        await websocket.close(code=4403, reason="Origin not allowed")
        return
    protocols = list(websocket.scope.get("subprotocols") or [])
    api_key = websocket.headers.get("x-api-key") or _api_key_from_protocols(protocols)
    if not await credentials_ok(websocket.cookies.get(auth.SESSION_COOKIE), api_key):
        await websocket.close(code=4401, reason="Authentication required")
        return

    session = get_profile_manager().browser_sessions.active_sessions.get(profile_id)
    endpoint = session.cdp_endpoint if session is not None else None
    if endpoint is None:
        await websocket.close(code=4404, reason="This profile has no browser to drive")
        return

    try:
        # No size cap and no pings: CDP messages carry whole screenshots, and
        # the client runs its own keepalive across the relay. No Origin header
        # either, which is what Chrome's --remote-allow-origins check accepts.
        upstream = await connect(endpoint, max_size=None, ping_interval=None, open_timeout=10)
    except (OSError, TimeoutError, ConnectionClosed) as exc:
        logger.warning(f"CDP endpoint of {profile_id} refused the relay: {exc}")
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
