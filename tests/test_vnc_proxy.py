import asyncio
import socketserver
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tra_sniper.api import create_app
from tra_sniper.auth import TokenManager
from tra_sniper.browser_session import BookingSessionManager, SessionBusyError
from tra_sniper.storage import Database
from tra_sniper.vnc_proxy import relay_vnc


@pytest.mark.parametrize("expire", [False, True])
def test_live_stream_is_revoked_before_the_next_session(expire):
    async def check():
        received = asyncio.Queue()
        disconnected = asyncio.Event()

        async def vnc(reader, writer):
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            try:
                while data := await reader.read(1024):
                    received.put_nowait(data)
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                disconnected.set()

        server = await asyncio.start_server(vnc, "127.0.0.1", 0)
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        websocket = AsyncMock()
        websocket.scope = {"subprotocols": ["binary", "base64"]}
        websocket.receive_bytes.side_effect = incoming.get
        websocket.send_bytes.side_effect = outgoing.put
        sessions = BookingSessionManager()
        session = sessions.acquire("first", 1)
        task = asyncio.create_task(relay_vnc(
            websocket, sessions, session.token, "127.0.0.1", server.sockets[0].getsockname()[1],
        ))
        try:
            assert await asyncio.wait_for(outgoing.get(), 2) == b"RFB 003.008\n"
            websocket.accept.assert_awaited_once_with(subprotocol="binary")
            incoming.put_nowait(b"test keyboard input")
            assert await asyncio.wait_for(outgoing.get(), 2) == b"test keyboard input"
            assert await received.get() == b"test keyboard input"
            if expire:
                session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                sessions.release(session.token)
            assert sessions.resolve(session.token) is None
            with pytest.raises(SessionBusyError):
                sessions.acquire("second", 2)
            incoming.put_nowait(b"must not reach the next session")
            await asyncio.wait_for(task, 2)
            await asyncio.wait_for(disconnected.wait(), 2)
            assert received.empty()
            assert session.streams == 0
            if expire:
                sessions.release(session.token)  # Worker cleanup is still required.
            sessions.acquire("second", 2)
            assert sessions.resolve(session.token) is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()

    asyncio.run(check())


def test_invalid_token_never_connects_to_vnc(monkeypatch):
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    websocket = AsyncMock()
    asyncio.run(relay_vnc(websocket, BookingSessionManager(), "invalid", "127.0.0.1"))
    connect.assert_not_awaited()
    websocket.accept.assert_not_awaited()
    websocket.close.assert_awaited_once_with(code=1008)


def test_api_websocket_routes_binary_vnc_and_revokes_it(tmp_path, monkeypatch):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b'RFB 003.008\n')
            while data := self.request.recv(1024):
                self.request.sendall(data)

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        original_connect = asyncio.open_connection

        async def connect(host, port):
            assert host == '127.0.0.1' and port == 5900
            return await original_connect(host, server.server_address[1])

        monkeypatch.setattr(asyncio, 'open_connection', connect)
        monkeypatch.setenv('TRA_BROWSER_CDP_URL', 'http://127.0.0.1:9222')
        db = Database(tmp_path / 'vnc.db', encryption_key=Fernet.generate_key().decode())
        app = create_app(db, TokenManager('t' * 32), start_scheduler=False)
        sessions = app.state.booking_sessions
        session = sessions.acquire('first', 1)
        detached = threading.Event()
        original_detach = sessions.detach_stream

        def detach(value):
            original_detach(value)
            detached.set()

        monkeypatch.setattr(sessions, 'detach_stream', detach)
        path = f'/booking-session/{session.token}/websockify'
        try:
            with TestClient(app) as client:
                with client.websocket_connect(path, subprotocols=['binary']) as websocket:
                    assert websocket.accepted_subprotocol == 'binary'
                    assert websocket.receive_bytes() == b'RFB 003.008\n'
                    websocket.send_bytes(b'test')
                    assert websocket.receive_bytes() == b'test'
                    sessions.release(session.token)
                    with pytest.raises(WebSocketDisconnect):
                        websocket.receive_bytes()
                assert detached.wait(1)
                sessions.acquire('second', 2)
                with pytest.raises(WebSocketDisconnect), client.websocket_connect(path):
                    pytest.fail('old token must not reconnect')
        finally:
            server.shutdown()
            worker.join(2)
