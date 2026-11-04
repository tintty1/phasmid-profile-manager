"""The VNC relay: who may connect, and that bytes cross it both ways.

A plain TCP echo server stands in for Xvnc, so no display is started.
"""

import socket
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from camoufox_pm.api.routes import viewer
from camoufox_pm.main import app

URL = "/api/v1/profiles/p1/vnc"


@pytest.fixture
def echo_port():
    """A loopback server that echoes back whatever it receives."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def relay(monkeypatch, echo_port):
    """The relay with auth and the display registry replaced by stand-ins."""
    state = {"authorized": True, "display": SimpleNamespace(name=":100", rfb_port=echo_port)}

    async def credentials_ok(_token, _key):
        return state["authorized"]

    displays = SimpleNamespace(get=lambda _pid: state["display"])
    manager = SimpleNamespace(browser_sessions=SimpleNamespace(vnc=displays))
    monkeypatch.setattr(viewer, "credentials_ok", credentials_ok)
    monkeypatch.setattr(viewer, "get_profile_manager", lambda: manager)
    return state


def test_relays_bytes_both_ways(relay):
    with TestClient(app).websocket_connect(URL, subprotocols=["binary"]) as ws:
        assert ws.accepted_subprotocol == "binary"
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"RFB 003.008\n"


def test_key_subprotocol_is_never_echoed(relay):
    with TestClient(app).websocket_connect(URL, subprotocols=["binary", "cpm-key.c2VjcmV0"]) as ws:
        assert ws.accepted_subprotocol == "binary"


def test_refuses_without_credentials(relay):
    relay["authorized"] = False
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4401


def test_refuses_a_page_from_another_site(relay):
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        TestClient(app).websocket_connect(URL, headers={"origin": "https://evil.example"}),
    ):
        pass
    assert exc.value.code == 4403


def test_accepts_the_page_it_served(relay):
    with TestClient(app).websocket_connect(URL, headers={"origin": "http://testserver"}) as ws:
        ws.send_bytes(b"x")
        assert ws.receive_bytes() == b"x"


def test_profile_without_a_display(relay):
    relay["display"] = None
    with pytest.raises(WebSocketDisconnect) as exc, TestClient(app).websocket_connect(URL):
        pass
    assert exc.value.code == 4404
