"""Authenticated WebSocket admission and owned signalling-session lifecycle."""

import asyncio
import time
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from .auth import AuthConfig, AuthError, Authority, AuthorizationSession, TicketVerifier
from .config import Settings
from .contracts import ContractError, decode_message
from .signalling import CloseSession, DispatchMetrics, SocketSession

if TYPE_CHECKING:
    from .media import MediaHub

PROTOCOL = "jrtc.signalling.v1"
BINDING = ("meeting_id", "participant_session_id", "generation")


class RateLimit:
    def __init__(self, *, burst: int, per_second: float) -> None:
        self.burst, self.per_second = burst, per_second
        self.tokens = float(burst)
        self.updated = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.per_second)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class AuthenticatedEndpoint:
    def __init__(
        self,
        config: AuthConfig,
        settings: Settings,
        authority: Authority,
        media: "MediaHub | None" = None,
    ) -> None:
        self.config, self.settings, self.authority = config, settings, authority
        self.verifier = TicketVerifier(config)
        self.media = media
        self.metrics = DispatchMetrics()
        self.operations = asyncio.Semaphore(16)
        self.handlers: set[asyncio.Task[Any]] = set()
        self.connections: dict[WebSocket, AuthorizationSession | None] = {}
        self.handshakes = RateLimit(burst=20, per_second=10)
        self.closing = False

    async def deny(self, websocket: WebSocket, code: str, status: int = 403) -> None:
        if "websocket.http.response" in websocket.scope.get("extensions", {}):
            await websocket.send_denial_response(
                JSONResponse(
                    {"error": {"code": code, "message": "Connection denied."}},
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )
            )
        else:
            await websocket.close(code=1008, reason="Connection denied.")

    async def _receive(self, websocket: WebSocket, budget: float) -> dict[str, Any]:
        async with asyncio.timeout(budget):
            message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        text = message.get("text")
        if not isinstance(text, str):
            raise ContractError("Invalid frame.")
        if len(text.encode("utf-8")) > self.settings.websocket_max_size:
            raise CloseSession(1009)
        return decode_message(text)

    @staticmethod
    def _bind(request: dict[str, Any], claims: dict[str, Any], sequence: int) -> None:
        if any(request[key] != claims[key] for key in BINDING):
            raise AuthError("FORBIDDEN")
        if request["sequence"] != sequence:
            raise AuthError("OUT_OF_ORDER")

    async def __call__(self, websocket: WebSocket) -> None:
        origins = websocket.headers.getlist("origin")
        if (
            len(origins) != 1
            or origins[0] not in self.config.origins
            or websocket.scope.get("scheme") != "wss"
            or websocket.scope.get("query_string")
            or websocket.scope.get("subprotocols") != [PROTOCOL]
        ):
            await self.deny(websocket, "FORBIDDEN")
            return
        if (
            self.closing
            or len(self.connections) >= self.config.max_connections
            or not self.handshakes.take()
        ):
            await self.deny(websocket, "OVERLOADED", 503)
            return
        # No await between admission and insertion: bounded per event-loop worker.
        self.connections[websocket] = None
        handler = asyncio.current_task()
        assert handler is not None
        self.handlers.add(handler)
        try:
            async with asyncio.timeout(self.config.auth_timeout):
                await websocket.accept(subprotocol=PROTOCOL)
                request = await self._receive(websocket, self.config.auth_timeout)
                if request["op"] not in {"session.hello", "session.resume"}:
                    raise AuthError()
                claims = self.verifier.verify(request["payload"]["ticket"])
                self._bind(request, claims, 1)
                if request["op"] == "session.resume" and "session:resume" not in claims["scopes"]:
                    raise AuthError("FORBIDDEN")
                session = AuthorizationSession(
                    self.authority, claims, cache_seconds=self.config.cache_seconds
                )
                await session.check(request["op"], consume=True)
            self.connections[websocket] = session
            await SocketSession(self, websocket, session).run(request)
        except WebSocketDisconnect:
            pass
        except CloseSession as exc:
            await self._close(websocket, exc.code)
        except AuthError as exc:
            code = 1013 if exc.code in {"UNAVAILABLE", "OVERLOADED", "RATE_LIMITED"} else 1008
            await self._close(websocket, code)
        except ContractError:
            await self._close(websocket, 1008)
        except TimeoutError:
            await self._close(websocket, 1008)
        except Exception:
            # Do not serialize or log exception text containing tokens/HTTP bodies.
            await self._close(websocket, 1011)
        finally:
            self.connections.pop(websocket, None)
            self.handlers.discard(handler)

    async def _close(self, websocket: WebSocket, code: int) -> None:
        try:
            async with asyncio.timeout(2):
                await websocket.close(code=code, reason="Session closed.")
        except (RuntimeError, TimeoutError, WebSocketDisconnect):
            pass

    async def aclose(self) -> None:
        self.closing = True
        await asyncio.gather(
            *(self._close(ws, 1012 if self.media else 1001) for ws in list(self.connections))
        )
        handlers = tuple(self.handlers)
        for task in handlers:
            task.cancel()
        async with asyncio.timeout(self.settings.shutdown_timeout):
            await asyncio.gather(*handlers, return_exceptions=True)
            if self.media is not None:
                await self.media.aclose()
            await self.authority.aclose()
