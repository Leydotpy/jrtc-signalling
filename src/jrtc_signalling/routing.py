"""Forward a verified hello over one direct TLS hop. Only the owner consumes it."""

import asyncio
import json
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .auth import AuthError
from .config import Settings
from .multiworker import Elections
from .websocket_auth import PROTOCOL

HOP = "x-jrtc-owner-hop"


class DirectConnection(connect):
    def process_redirect(self, exc: Exception) -> Exception | str:
        # The configured direct TLS endpoint is the entire credential trust boundary.
        return exc


async def resolve(elections: Elections, meeting: str, websocket: WebSocket) -> str | None:
    try:
        runtime = elections.runtime
        if runtime.retiring or runtime.stopping.is_set():
            raise AuthError("UNAVAILABLE")
        route = await elections.lookup(meeting)
        if route.owner == runtime.owner_id:
            if route.incarnation != runtime.incarnation:
                raise AuthError("UNAVAILABLE")
            runtime.require(meeting, route.generation)
            return None
        if HOP in websocket.headers:
            raise AuthError("UNAVAILABLE")
        return elections.routes[route.owner]
    except Exception:
        raise AuthError("UNAVAILABLE") from None


def close_code(code: int | None) -> int:
    if code in {1000, 1001, 1008, 1009, 1011, 1012, 1013, 4001, 4009}:
        return int(code)
    return 1013


async def relay(
    browser: WebSocket,
    target: str,
    hello: dict[str, Any],
    settings: Settings,
    handshake_budget: float,
) -> None:
    try:
        async with DirectConnection(
            target,
            origin=browser.headers["origin"],
            subprotocols=[PROTOCOL],
            additional_headers={HOP: "1"},
            open_timeout=handshake_budget,
            close_timeout=1,
            compression=None,
            max_size=settings.websocket_max_size,
            max_queue=settings.websocket_max_queue,
            proxy=None,
        ) as upstream:
            if upstream.subprotocol != PROTOCOL:
                raise AuthError("UNAVAILABLE")
            await upstream.send(json.dumps(hello, separators=(",", ":")))

            async def outbound() -> None:
                while True:
                    message = await browser.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close(close_code(message.get("code")))
                        return
                    raw = message.get("text")
                    if not isinstance(raw, str) or len(raw.encode()) > settings.websocket_max_size:
                        await browser.close(code=1009)
                        return
                    await upstream.send(raw)

            async def inbound() -> None:
                try:
                    while True:
                        raw = await upstream.recv()
                        if not isinstance(raw, str):
                            raise AuthError("UNAVAILABLE")
                        await browser.send_text(raw)
                except ConnectionClosed as exc:
                    await browser.close(code=close_code(exc.rcvd.code if exc.rcvd else None))

            tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        return
    except Exception:
        raise AuthError("UNAVAILABLE") from None
