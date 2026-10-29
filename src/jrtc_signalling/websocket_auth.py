"""Bounded authenticated control sessions; media dispatch remains SG-08 work."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from starlette.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from .auth import AuthConfig, AuthError, Authority, AuthorizationSession, TicketVerifier
from .config import Settings
from .contracts import ContractError, decode_message, validate_message

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
    def __init__(self, config: AuthConfig, settings: Settings, authority: Authority) -> None:
        self.config, self.settings, self.authority = config, settings, authority
        self.verifier = TicketVerifier(config)
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
        if (
            not isinstance(text, str)
            or len(text.encode("utf-8")) > self.settings.websocket_max_size
        ):
            raise ContractError("Invalid frame.")
        return decode_message(text)

    async def _reply(
        self, websocket: WebSocket, request: dict[str, Any], error: str | None = None
    ) -> None:
        result: dict[str, Any] = {
            "version": 1,
            "id": request["id"],
            "generation": request["generation"],
            "server_time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "ok": error is None,
        }
        if error:
            result["error"] = {
                "code": error,
                "message": "Operation unavailable.",
                "retryable": error == "UNAVAILABLE",
            }
        else:
            # No invented owner generation or media-ready claim before SG-07/08.
            result["result"] = {}
        validate_message(result, schema="response")
        async with asyncio.timeout(2):
            await websocket.send_json(result)

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
            await self._reply(websocket, request)
            sequence = 2
            rate = RateLimit(burst=60, per_second=30)
            while not self.closing:
                remaining = session.claims["exp"] - time.time()
                if remaining <= 0:
                    raise AuthError("EXPIRED_TICKET")
                request = await self._receive(websocket, remaining)
                if time.time() >= session.claims["exp"]:
                    raise AuthError("EXPIRED_TICKET")
                if not rate.take():
                    raise AuthError("RATE_LIMITED")
                self._bind(request, session.claims, sequence)
                sequence += 1
                operation = request["op"]
                if operation == "session.refresh":
                    revision = session.revision
                    claims = self.verifier.verify(request["payload"]["ticket"])
                    identity = (*BINDING, "sub", "participant_id", "grant_id")
                    if (
                        any(claims[key] != session.claims[key] for key in identity)
                        or claims["authorization_epoch"] < session.minimum_epoch
                    ):
                        raise AuthError("FORBIDDEN")
                    fresh = AuthorizationSession(
                        self.authority, claims, cache_seconds=self.config.cache_seconds
                    )
                    await fresh.check(operation, consume=True)
                    if session.revision != revision:
                        raise AuthError("STALE_AUTHORIZATION")
                    session = fresh
                    self.connections[websocket] = session
                    await self._reply(websocket, request)
                elif operation == "session.ping":
                    await session.check(operation)
                    await self._reply(websocket, request)
                elif operation.startswith("media."):
                    await session.authorize(operation, request["payload"])
                    await self._reply(websocket, request, "UNAVAILABLE")
                else:
                    raise AuthError("FORBIDDEN")
        except WebSocketDisconnect:
            pass
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

    async def _close(self, websocket: WebSocket, code: int) -> None:
        try:
            async with asyncio.timeout(2):
                await websocket.close(code=code, reason="Session closed.")
        except (RuntimeError, TimeoutError, WebSocketDisconnect):
            pass

    async def aclose(self) -> None:
        self.closing = True
        await asyncio.gather(*(self._close(ws, 1001) for ws in list(self.connections)))
        async with asyncio.timeout(self.settings.shutdown_timeout):
            await self.authority.aclose()
