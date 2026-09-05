"""Session-scoped binary WebSocket ↔ VNC relay, with revocation of live streams."""

import asyncio
from contextlib import suppress

from fastapi import WebSocket, WebSocketDisconnect

from .browser_session import BookingSessionManager


async def relay_vnc(websocket: WebSocket, sessions: BookingSessionManager,
                    token: str, host: str, port: int = 5900) -> None:
    session = sessions.attach_stream(token)
    if session is None:
        await websocket.close(code=1008)
        return
    writer = None
    tasks: list[asyncio.Task] = []
    try:
        protocols = websocket.scope.get("subprotocols", [])
        await websocket.accept(subprotocol="binary" if "binary" in protocols else None)

        async def relay() -> None:
            nonlocal writer
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)

            async def to_browser() -> None:
                while sessions.resolve(token) is session:
                    data = await websocket.receive_bytes()
                    if sessions.resolve(token) is not session:
                        return
                    writer.write(data)
                    await writer.drain()

            async def to_viewer() -> None:
                while sessions.resolve(token) is session:
                    data = await reader.read(65536)
                    if not data or sessions.resolve(token) is not session:
                        return
                    await websocket.send_bytes(data)

            pumps = [asyncio.create_task(to_browser()), asyncio.create_task(to_viewer())]
            try:
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in pumps:
                    task.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)

        async def revoked() -> None:
            while sessions.resolve(token) is session:
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(relay()), asyncio.create_task(revoked())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (OSError, WebSocketDisconnect, TimeoutError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if writer is not None:
            # Abort discards queued input, unlike a graceful TCP close. The next
            # session cannot acquire the slot until this stream is detached.
            writer.transport.abort()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)
        with suppress(RuntimeError, OSError, TimeoutError):
            await asyncio.wait_for(websocket.close(code=1000), 2)
        sessions.detach_stream(session)
