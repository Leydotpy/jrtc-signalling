"""Authenticated WebSocket admission and owned signalling-session lifecycle."""

import asyncio
import time
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from .auth import AuthConfig, AuthError, Authority, AuthorizationSession, TicketVerifier
from .config import Settings
from .contracts import ContractError, decode_message
from .reconnect import ConnectionHandoffs
from .signalling import CloseSession, DispatchMetrics, SocketSession
from .telemetry import GatewayTelemetry

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
        *,
        event_runtime: Any = None,
        telemetry: GatewayTelemetry | None = None,
    ) -> None:
        self.config, self.settings, self.authority = config, settings, authority
        self.verifier = TicketVerifier(config)
        self.media = media
        self.event_runtime = event_runtime
        self.metrics = DispatchMetrics()
        self.telemetry = telemetry or GatewayTelemetry()
        self.operations = asyncio.Semaphore(16)
        self.handlers: set[asyncio.Task[Any]] = set()
        self.connections: dict[WebSocket, AuthorizationSession | None] = {}
        self.sessions: set[SocketSession] = set()
        self.handoffs = ConnectionHandoffs()
        self.moderation = None
        if media is not None:
            from .moderation import ModerationEnforcer

            self.moderation = ModerationEnforcer(self)
            media.runtime.moderation = self.moderation
        self.elections = media.runtime.elections if media is not None else None
        if self.elections is not None and media is not None:
            media.runtime.close_connections = self.aclose
        self.handshakes = RateLimit(burst=20, per_second=10)
        self.closing = False
        self.close_task: asyncio.Task[None] | None = None

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
            self.telemetry.handshake(success=False)
            self.telemetry.auth_error("FORBIDDEN")
            await self.deny(websocket, "FORBIDDEN")
            return
        if (
            self.closing
            or len(self.connections) >= self.config.max_connections
            or not self.handshakes.take()
        ):
            self.telemetry.handshake(success=False)
            await self.deny(websocket, "OVERLOADED", 503)
            return
        # No await between admission and insertion: bounded per event-loop worker.
        self.connections[websocket] = None
        handler = asyncio.current_task()
        assert handler is not None
        self.handlers.add(handler)
        socket_session = None
        resume = relayed = False
        try:
            async with asyncio.timeout(self.config.auth_timeout):
                await websocket.accept(subprotocol=PROTOCOL)
                request = await self._receive(websocket, self.config.auth_timeout)
                if request["op"] not in {"session.hello", "session.resume"}:
                    raise AuthError()
                resume = request["op"] == "session.resume"
                claims = self.verifier.verify(request["payload"]["ticket"])
                self._bind(request, claims, 1)
                if request["op"] == "session.resume" and "session:resume" not in claims["scopes"]:
                    raise AuthError("FORBIDDEN")
                target = None
                if self.elections is not None:
                    from .routing import resolve

                    target = await resolve(self.elections, claims["meeting_id"], websocket)
                if target is None:
                    session = AuthorizationSession(
                        self.authority, claims, cache_seconds=self.config.cache_seconds
                    )
                    await session.check(request["op"], consume=True)
            if target is not None:
                from .routing import relay

                relayed = True
                self.telemetry.relayed += 1
                await relay(websocket, target, request, self.settings, self.config.auth_timeout)
                return
            self.connections[websocket] = session
            if self.moderation is not None:
                self.moderation.check(session)
            socket_session = SocketSession(self, websocket, session)
            self.sessions.add(socket_session)
            await self.handoffs.claim(socket_session)
            await socket_session.run(request)
        except WebSocketDisconnect:
            pass
        except CloseSession as exc:
            await self._close(websocket, exc.code)
        except AuthError as exc:
            self.telemetry.auth_error(exc.code)
            code = 1013 if exc.code in {"UNAVAILABLE", "OVERLOADED", "RATE_LIMITED"} else 1008
            if socket_session is not None and socket_session.superseded:
                code = 4009
            elif exc.code == "EXPIRED_TICKET":
                code = 4001
            await self._close(websocket, code)
        except ContractError:
            await self._close(websocket, 1008)
        except TimeoutError:
            await self._close(websocket, 1008)
        except Exception:
            # Do not serialize or log exception text containing tokens/HTTP bodies.
            await self._close(websocket, 1011)
        finally:
            if not relayed and (socket_session is None or not socket_session.admitted):
                self.telemetry.handshake(success=False, resume=resume)
            self.connections.pop(websocket, None)
            self.handlers.discard(handler)
            if socket_session is not None:
                self.sessions.discard(socket_session)
                socket_session.finished.set()
                self.handoffs.release(socket_session)

    async def _close(self, websocket: WebSocket, code: int) -> None:
        try:
            async with asyncio.timeout(2):
                await websocket.close(code=code, reason="Session closed.")
        except (RuntimeError, TimeoutError, WebSocketDisconnect):
            pass

    async def aclose(self) -> None:
        if self.close_task is None:
            self.closing = True
            self.close_task = asyncio.create_task(self._close_all(), name="gateway-websocket-close")
        await asyncio.shield(self.close_task)

    async def _close_all(self) -> None:
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
