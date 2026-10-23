"""Cookie-authenticated v1 hints and explicitly registered peer signalling."""
import asyncio
import json
import sqlite3
import time

from anyio import CancelScope

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from .auth import AuthError
from .store import StoreError
from .sync_notifications import MAX_MESSAGE_BYTES
from .peer_contracts import PeerProtocolError, parse_message
from .peer_registry import RegistryError

SEND_TIMEOUT_SECONDS = 5


class SocketEnd(Exception):
    def __init__(self, code):
        self.code = code


def install_sync_socket(app, hub, auth_service, peer_service=None):
    @app.websocket("/api/sync/events")
    async def events(socket: WebSocket):
        subscription, peer_connection, tasks, handshake = None, None, [], False
        next_heartbeat = 0
        sent_cursor = -1
        code = 1000
        sid = socket.cookies.get(auth_service.cookie_name)
        origin = socket.headers.get("origin")

        async def authorize():
            session = await asyncio.wait_for(asyncio.to_thread(auth_service.websocket_session, sid, origin), SEND_TIMEOUT_SECONDS)
            if subscription is not None and (session["user"]["id"] != subscription.user_id
                                            or session["sessionKey"] != subscription.session_key):
                raise SocketEnd(1008)
            return session

        async def send(kind):
            nonlocal next_heartbeat, sent_cursor
            next_heartbeat = time.monotonic() + hub.heartbeat_seconds
            await authorize()
            if subscription.close_code is not None:
                raise SocketEnd(subscription.close_code)
            cursor = subscription.cursor
            raw = json.dumps({"v": 1, "type": kind, "serverId": hub.server_id,
                "userId": subscription.user_id, "cursor": cursor}, ensure_ascii=False, separators=(",", ":"))
            if len(raw.encode()) > MAX_MESSAGE_BYTES:
                raise SocketEnd(1011)
            await asyncio.wait_for(socket.send_text(raw), SEND_TIMEOUT_SECONDS)
            sent_cursor = cursor

        async def writer():
            watermark = await asyncio.to_thread(hub.store.watermark, subscription.user_id)
            if watermark["serverId"] != hub.server_id:
                raise SocketEnd(1012)
            subscription.hint(watermark["cursor"])
            subscription.changed.clear()
            await send("sync.ready")
            while True:
                try:
                    await asyncio.wait_for(subscription.changed.wait(), max(0, next_heartbeat - time.monotonic()))
                except TimeoutError:
                    pass
                subscription.changed.clear()
                if subscription.close_code is not None:
                    raise SocketEnd(subscription.close_code)
                if subscription.cursor > sent_cursor or time.monotonic() >= next_heartbeat:
                    await send("sync.hint")
                if peer_connection is not None:
                    for _ in range(8):
                        if subscription.cursor > sent_cursor or time.monotonic() >= next_heartbeat:
                            await send("sync.hint")
                        message = peer_connection.pop()
                        if message is None:
                            break
                        await authorize()
                        if subscription.close_code is not None:
                            raise SocketEnd(subscription.close_code)
                        message = await asyncio.wait_for(peer_service.prepare_outgoing(peer_connection, message), SEND_TIMEOUT_SECONDS)
                        await asyncio.wait_for(socket.send_text(json.dumps(message, ensure_ascii=False, separators=(",", ":"))), SEND_TIMEOUT_SECONDS)
                    if peer_connection.queue:
                        subscription.changed.set()

        async def reader():
            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                raw = message.get("bytes")
                size = len(raw) if raw is not None else len(message.get("text", "").encode())
                if peer_connection is None:
                    raise SocketEnd(1009 if size > MAX_MESSAGE_BYTES else 1008)
                command = parse_message(message.get("text") if raw is None else raw)
                await authorize()
                await asyncio.wait_for(peer_service.dispatch(peer_connection, command), SEND_TIMEOUT_SECONDS)

        try:
            if socket.url.query:
                raise SocketEnd(1008)
            hub.begin_handshake()
            handshake = True
            session = await authorize()
            subscription = hub.register(session["user"]["id"], session["sessionKey"])
            if peer_service is not None:
                peer_connection = peer_service.attach(subscription)
            hub.end_handshake()
            handshake = False
            await socket.accept()
            tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except SocketEnd as error:
            code = error.code
        except PeerProtocolError as error:
            code = error.close_code
        except RegistryError as error:
            code = 1013 if error.code in ("unavailable", "capacity") else 1008
        except AuthError as error:
            code = 1008 if error.status in (401, 403) else 1013
        except StoreError as error:
            code = 1008 if error.status_code in (401, 403) else 1013
        except (TimeoutError, sqlite3.Error):
            code = 1013
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            code = 1013
        except asyncio.CancelledError:
            pass
        finally:
            with CancelScope(shield=True):
                if handshake:
                    hub.end_handshake()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if peer_connection is not None:
                    await peer_service.detach(peer_connection)
                if subscription is not None:
                    hub.unregister(subscription)
                try:
                    await asyncio.wait_for(socket.close(code=code), SEND_TIMEOUT_SECONDS)
                except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
                    pass
