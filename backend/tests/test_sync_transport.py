"""Pinned production WS engine against a temporary loopback TCP listener."""
import asyncio
import base64
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest
import uvicorn
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from test_sync_socket import socket_app


def test_pinned_uvicorn_transport_handshake_hints_limits_and_cleanup():
    assert importlib.metadata.version("websockets") == "17.3"
    app, hub, store, auth = socket_app(heartbeat=0.05)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(16)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, ws="websockets-sansio",
        ws_max_size=65536, ws_ping_interval=0.05, ws_ping_timeout=0.05, ws_per_message_deflate=False,
        log_level="error", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True,
                              name="test-sync-loopback-uvicorn")
    thread.start()
    url = f"ws://127.0.0.1:{port}/api/sync/events"
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with pytest.raises(InvalidStatus) as error:
            with connect(url, origin="http://localhost", proxy=None, open_timeout=2):
                pass
        assert error.value.response.status_code == 403
        with connect(url, origin="http://localhost", additional_headers={"Cookie": "fixture_session=alice"},
                     proxy=None, open_timeout=2, close_timeout=2) as ws:
            ready = json.loads(ws.recv(timeout=2))
            assert ready["type"] == "sync.ready" and ready["cursor"] == 0
            # SQL-only advance simulates the crash-before-publish / lost notification gap.
            store.commit("alice", 6, notify=False)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                hint = json.loads(ws.recv(timeout=2))
                if hint["cursor"] == 6:
                    break
            assert hint["type"] == "sync.hint" and hint["cursor"] == 6
            ws.send("x" * 4097)
            with pytest.raises(ConnectionClosed) as closed:
                while True:
                    ws.recv(timeout=2)
            assert closed.value.rcvd.code == 1009
        # A raw upgraded client intentionally never answers ping. This proves the
        # pinned Sans-IO engine actually uses the configured keepalive deadlines.
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            key = base64.b64encode(os.urandom(16)).decode()
            request = (f"GET /api/sync/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nOrigin: http://localhost\r\nCookie: fixture_session=alice\r\n\r\n")
            raw.sendall(request.encode())
            with raw.makefile("rb") as stream:
                assert b"101" in stream.readline()
                while stream.readline() != b"\r\n":
                    pass
                saw_ping = False
                while True:
                    first, second = stream.read(2)
                    length = second & 127
                    if length == 126:
                        length = int.from_bytes(stream.read(2), "big")
                    elif length == 127:
                        length = int.from_bytes(stream.read(8), "big")
                    payload = stream.read(length)
                    opcode = first & 15
                    saw_ping |= opcode == 9
                    if opcode == 8:
                        assert saw_ping and int.from_bytes(payload[:2], "big") == 1011
                        break
    finally:
        server.should_exit = True
        thread.join(5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(3)
        listener.close()
        assert not thread.is_alive(), "Owned loopback Uvicorn thread must stop"
        assert not hub.connections and not store.listeners and not auth.listeners
        probe = socket.socket()
        probe.settimeout(0.2)
        try:
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "Test port must be closed"
        finally:
            probe.close()


def test_production_proxy_and_engine_use_dedicated_bounded_websocket_path():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "deploy/ubuntu/memoverse.service").read_text()
    assert "--ws websockets-sansio --ws-max-size 65536" in unit
    assert "--ws-ping-interval 15 --ws-ping-timeout 15 --ws-per-message-deflate false" in unit
    proxy = (root / "deploy/ubuntu/nginx.conf.template").read_text()
    location = proxy.split("location = /api/sync/events {", 1)[1].split("}", 1)[0]
    for required in ('proxy_set_header Upgrade $http_upgrade;', 'proxy_set_header Connection "upgrade";',
                     'proxy_set_header Host $host;', 'proxy_set_header X-Forwarded-Proto $scheme;',
                     'proxy_read_timeout 45s;', 'proxy_pass http://127.0.0.1:8000;'):
        assert required in location
