"""The CDP relay: who may connect, and that messages cross it intact.

A WebSocket echo server stands in for the browser's CDP endpoint, so no
browser is launched.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from phasmid_pm.api.routes import automation
from phasmid_pm.core.download_watch import DownloadRegistry
from phasmid_pm.main import app

URL = "/api/v1/profiles/p1/cdp"


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
def relay(monkeypatch, echo_endpoint, tmp_path):
    """The relay with auth and the session registry replaced by stand-ins."""
    state = {
        "authorized": True,
        "sessions": {"p1": SimpleNamespace(cdp_endpoint=echo_endpoint)},
    }

    async def credentials_ok(_token, _key):
        return state["authorized"]

    state["downloads"] = tmp_path / "downloads" / "p1"
    manager = SimpleNamespace(
        browser_sessions=SimpleNamespace(active_sessions=state["sessions"]),
        downloads_dir=lambda _profile_id: state["downloads"],
        download_registry=DownloadRegistry(),
        cdp_relays={},
    )
    state["manager"] = manager
    monkeypatch.setattr(automation, "credentials_ok", credentials_ok)
    monkeypatch.setattr(automation, "get_profile_manager", lambda: manager)
    return state


def test_relays_text_frames_as_text(relay):
    """CDP is JSON in text frames; a binary echo would break it."""
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text('{"id":1,"method":"Browser.getVersion"}')
        assert ws.receive_text() == '{"id":1,"method":"Browser.getVersion"}'


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
    relay["sessions"]["p1"].cdp_endpoint = None
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4404


def test_a_clients_download_folder_is_replaced_by_the_profiles(relay):
    """Playwright asks for a folder on its own machine; the browser gets the server's."""
    asked = {
        "id": 7,
        "method": "Browser.setDownloadBehavior",
        "params": {"behavior": "allowAndName", "downloadPath": "/tmp/playwright-artifacts-x"},
    }
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text(json.dumps(asked))
        sent_on = json.loads(ws.receive_text())  # the echo: what the browser received
    assert sent_on["id"] == 7
    assert sent_on["params"] == {
        "behavior": "allowAndName",
        "downloadPath": str(relay["downloads"] / ".incoming"),
        "eventsEnabled": True,
    }


def test_deny_is_passed_through_untouched(relay):
    asked = {"id": 1, "method": "Browser.setDownloadBehavior", "params": {"behavior": "deny"}}
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text(json.dumps(asked))
        assert json.loads(ws.receive_text()) == asked


def test_a_finished_download_is_filed_under_its_own_name(relay):
    folder = relay["downloads"]
    (folder / ".incoming").mkdir(parents=True)
    (folder / "orders.csv").write_text("older")
    (folder / ".incoming" / "guid-1").write_text("newer")
    begin = {
        "method": "Browser.downloadWillBegin",
        "params": {"guid": "guid-1", "suggestedFilename": "orders.csv", "url": "https://x/o"},
    }
    done = {
        "method": "Browser.downloadProgress",
        "params": {"guid": "guid-1", "state": "completed"},
    }
    with TestClient(app).websocket_connect(URL) as ws:
        # The echo sends each back as if the browser had: the relay sees them
        # on the way to the client, exactly where real events pass.
        ws.send_text(json.dumps(begin))
        assert json.loads(ws.receive_text()) == begin
        ws.send_text(json.dumps(done))
        assert json.loads(ws.receive_text()) == done

    assert (folder / "orders.csv").read_text() == "older", "never overwritten"
    assert (folder / "orders (1).csv").read_text() == "newer"
    named = relay["manager"].download_registry.by_name("p1")
    assert named["orders (1).csv"].guid == "guid-1"


def test_the_last_client_to_leave_hands_downloads_back(relay):
    with TestClient(app).websocket_connect(URL) as ws:
        ws.send_text("{}")
        ws.receive_text()
    assert relay["manager"].cdp_relays == {}
