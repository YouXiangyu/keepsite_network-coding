"""Cookie-authenticated outbound hints. M5 signalling is not implemented here."""
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

SEND_TIMEOUT_SECONDS = 5


class SocketEnd(Exception):
    def __init__(self, code):
        self.code = code


def install_sync_socket(app, hub, auth_service):
    @app.websocket("/api/sync/events")
    async def events(socket: WebSocket):
        subscription, tasks, handshake = None, [], False
        next_heartbeat = 0
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
            nonlocal next_heartbeat
            next_heartbeat = time.monotonic() + hub.heartbeat_seconds
            await authorize()
            if subscription.close_code is not None:
                raise SocketEnd(subscription.close_code)
            raw = json.dumps({"v": 1, "type": kind, "serverId": hub.server_id,
                "userId": subscription.user_id, "cursor": subscription.cursor}, ensure_ascii=False, separators=(",", ":"))
            if len(raw.encode()) > MAX_MESSAGE_BYTES:
                raise SocketEnd(1011)
            await asyncio.wait_for(socket.send_text(raw), SEND_TIMEOUT_SECONDS)

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
                await send("sync.hint")

        async def reader():
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("bytes")
            size = len(raw) if raw is not None else len(message.get("text", "").encode())
            # Protocol ping/pong frames are consumed by Uvicorn; business input is forbidden.
            raise SocketEnd(1009 if size > MAX_MESSAGE_BYTES else 1008)

        try:
            if socket.url.query:
                raise SocketEnd(1008)
            hub.begin_handshake()
            handshake = True
            session = await authorize()
            subscription = hub.register(session["user"]["id"], session["sessionKey"])
            hub.end_handshake()
            handshake = False
            await socket.accept()
            tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except SocketEnd as error:
            code = error.code
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
                if subscription is not None:
                    hub.unregister(subscription)
                try:
                    await asyncio.wait_for(socket.close(code=code), SEND_TIMEOUT_SECONDS)
                except (RuntimeError, WebSocketDisconnect, TimeoutError, OSError):
                    pass
