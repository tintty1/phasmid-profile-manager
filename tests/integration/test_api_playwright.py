"""The Playwright relay: who may connect, and that messages cross it intact.

A WebSocket echo server stands in for the browser's Playwright server, so no
browser is launched.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from phasmid_pm.api.routes import automation
from phasmid_pm.main import app

URL = "/api/v1/profiles/p1/playwright"


@pytest.fixture
def echo_endpoint():
    """A loopback WebSocket server that echoes each message with its frame type."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    state: dict = {}

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    async def main():
        async with serve(echo, "127.0.0.1", 0, max_size=None) as server:
            state["port"] = server.sockets[0].getsockname()[1]
            state["stop"] = loop.create_future()
            ready.set()
            await state["stop"]

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), daemon=True)
    thread.start()
    ready.wait(5)
    yield f"ws://127.0.0.1:{state['port']}/guid"
    loop.call_soon_threadsafe(state["stop"].set_result, None)
    thread.join(5)


@pytest.fixture
def relay(monkeypatch, echo_endpoint):
    """The relay with auth and the session registry replaced by stand-ins."""
    state = {
        "authorized": True,
        "sessions": {"p1": SimpleNamespace(playwright_endpoint=echo_endpoint)},
    }

    async def credentials_ok(_token, _key):
        return state["authorized"]

    manager = SimpleNamespace(browser_sessions=SimpleNamespace(active_sessions=state["sessions"]))
    monkeypatch.setattr(automation, "credentials_ok", credentials_ok)
    monkeypatch.setattr(automation, "get_profile_manager", lambda: manager)
    return state


def test_relays_text_frames_as_text(relay):
    """Playwright's protocol is JSON in text frames; a binary echo would break it."""
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text('{"id":1,"method":"initialize"}')
        assert ws.receive_text() == '{"id":1,"method":"initialize"}'


def test_relays_binary_frames_as_binary(relay):
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"\x00\x01"


def test_relays_large_messages(relay):
    """A full-page screenshot arrives as one message well over 1 MiB."""
    big = "x" * (4 * 1024 * 1024)
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text(big)
        assert ws.receive_text() == big


def test_refuses_without_credentials(relay):
    relay["authorized"] = False
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4401


def test_refuses_a_page_from_another_site(relay):
    """Driving a profile is full control of it; another site must not get that."""
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        TestClient(app).websocket_connect(URL, headers={"origin": "https://evil.example"}),
    ):
        pass
    assert exc.value.code == 4403


def test_profile_that_is_not_running(relay):
    relay["sessions"].clear()
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4404


def test_browser_that_could_not_be_served(relay):
    relay["sessions"]["p1"].playwright_endpoint = None
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4404
