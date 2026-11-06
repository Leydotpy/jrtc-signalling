"""Ordered, bounded WebSocket request/reply and independent notification delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from starlette.websockets import WebSocket

from .auth import AuthError, AuthorizationSession
from .contracts import OperationError, validate_message
from .ownership import OwnershipError
from .schema_definitions import REQUEST_PAYLOADS

if TYPE_CHECKING:
    from .lifecycle_events import ObservationConnection
    from .media import MediaPeer
    from .websocket_auth import AuthenticatedEndpoint


class CloseSession(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__("Session closed.")


def encoded(message: dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class Timing:
    count: int = 0
    errors: int = 0
    total_seconds: float = 0
    maximum_seconds: float = 0
    queue_seconds: float = 0


@dataclass
class DispatchMetrics:
    # Fixed operation names only: no meeting IDs, URLs, tickets, SDP or ICE.
    operations: dict[str, Timing] = field(
        default_factory=lambda: {op: Timing() for op in REQUEST_PAYLOADS}
    )
    replayed: int = 0
    coalesced: int = 0
    overloads: int = 0
    active: int = 0
    peak_active: int = 0


@dataclass(repr=False)
class Record:
    digest: str
    reply: str | None = None


class SocketSession:
    REQUEST_LIMIT = 8
    REPLY_LIMIT = 16
    BYTE_LIMIT = 256 * 1024
    CACHE_LIMIT = 64
    REQUEST_SECONDS = 6.0

    def __init__(
        self,
        endpoint: AuthenticatedEndpoint,
        websocket: WebSocket,
        authorization: AuthorizationSession,
    ) -> None:
        self.endpoint, self.websocket, self.authorization = endpoint, websocket, authorization
        self.requests: asyncio.Queue[tuple[dict[str, Any], int, float]] = asyncio.Queue(
            maxsize=self.REQUEST_LIMIT
        )
        self.replies: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=self.REPLY_LIMIT)
        self.request_bytes = self.reply_bytes = self.cache_bytes = 0
        self.records: OrderedDict[str, Record] = OrderedDict()
        self.sequence = 2
        self.event_sequence = 0
        self.peer: MediaPeer | None = None
        self.observation: ObservationConnection | None = None
        self.transport_lost = False
        self.failure = asyncio.Event()
        self.metrics = endpoint.metrics

    def _put(self, raw: str) -> None:
        size = len(raw.encode())
        if self.reply_bytes + size > self.BYTE_LIMIT:
            raise AuthError("OVERLOADED")
        try:
            self.replies.put_nowait((raw, size))
        except asyncio.QueueFull:
            raise AuthError("OVERLOADED") from None
        self.reply_bytes += size

    def notify(self, op: str, payload: dict[str, Any]) -> None:
        self.event_sequence += 1
        claims = self.authorization.claims
        message = {
            "version": 1,
            "event_id": str(uuid4()),
            "op": op,
            "meeting_id": claims["meeting_id"],
            "participant_session_id": claims["participant_session_id"],
            "generation": claims["generation"],
            "sequence": self.event_sequence,
            "server_time": now(),
            "payload": payload,
        }
        try:
            validate_message(message, schema="notification")
            self._put(encoded(message))
        except (ValueError, AuthError):
            # Called from a bounded plugin callback: no websocket/broker await.
            self.metrics.overloads += 1
            self.failure.set()

    def _reply(
        self,
        request: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "version": 1,
            "id": request["id"],
            "ok": error is None,
            "generation": request["generation"],
            "server_time": now(),
        }
        if error is None:
            response["result"] = result if result is not None else {}
        else:
            response["error"] = {
                "code": error,
                "message": "Operation could not be completed.",
                "retryable": error in {"UNAVAILABLE", "OVERLOADED", "TIMEOUT"},
            }
        validate_message(response, schema="response")
        raw = encoded(response)
        self._put(raw)
        record = self.records[request["id"]]
        record.reply = raw
        self.cache_bytes += len(raw.encode())
        self._trim()

    def _trim(self) -> None:
        while len(self.records) > self.CACHE_LIMIT or self.cache_bytes > self.BYTE_LIMIT:
            oldest = next(
                (key for key, value in self.records.items() if value.reply is not None), None
            )
            if oldest is None:
                raise AuthError("OVERLOADED")
            record = self.records.pop(oldest)
            assert record.reply is not None
            self.cache_bytes -= len(record.reply.encode())

    @staticmethod
    def _digest(request: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def _reader(self) -> None:
        # Import here to keep the endpoint and session dependency direction simple.
        from .websocket_auth import BINDING, RateLimit

        rate = RateLimit(burst=60, per_second=30)
        while not self.endpoint.closing:
            auth = self.authorization
            remaining = auth.claims["exp"] - time.time()
            if remaining <= 0:
                raise AuthError("EXPIRED_TICKET")
            try:
                request = await self.endpoint._receive(self.websocket, remaining)
            except TimeoutError:
                # Refresh runs in the worker while this receive may still have
                # the previous ticket's deadline. Re-evaluate the current grant.
                if time.time() < self.authorization.claims["exp"]:
                    continue
                raise AuthError("EXPIRED_TICKET") from None
            if time.time() >= self.authorization.claims["exp"]:
                raise AuthError("EXPIRED_TICKET")
            if not rate.take():
                raise AuthError("RATE_LIMITED")
            if any(request[k] != self.authorization.claims[k] for k in BINDING):
                raise AuthError("FORBIDDEN")
            digest = self._digest(request)
            record = self.records.get(request["id"])
            if record is not None:
                if record.digest != digest or self.authorization.revoked:
                    raise AuthError("FORBIDDEN")
                if record.reply is None:
                    self.metrics.coalesced += 1
                else:
                    self.metrics.replayed += 1
                    self._put(record.reply)
                continue
            if request["sequence"] != self.sequence:
                raise AuthError("OUT_OF_ORDER")
            size = len(encoded(request).encode())
            if self.request_bytes + size > self.BYTE_LIMIT:
                raise AuthError("OVERLOADED")
            try:
                self.requests.put_nowait((request, size, time.monotonic()))
            except asyncio.QueueFull:
                raise AuthError("OVERLOADED") from None
            self.request_bytes += size
            self.records[request["id"]] = Record(digest)
            self._trim()
            self.sequence += 1

    async def _writer(self) -> None:
        while True:
            raw, size = await self.replies.get()
            try:
                async with asyncio.timeout(2):
                    await self.websocket.send_text(raw)
            except TimeoutError:
                raise CloseSession(1013) from None
            finally:
                self.reply_bytes -= size
                self.replies.task_done()

    async def _refresh(self, request: dict[str, Any]) -> None:
        from .websocket_auth import BINDING

        old = self.authorization
        revision = old.revision
        claims = self.endpoint.verifier.verify(request["payload"]["ticket"])
        if (
            any(claims[k] != old.claims[k] for k in (*BINDING, "sub", "participant_id", "grant_id"))
            or claims["authorization_epoch"] < old.minimum_epoch
        ):
            raise AuthError("FORBIDDEN")
        fresh = AuthorizationSession(
            self.endpoint.authority, claims, cache_seconds=self.endpoint.config.cache_seconds
        )
        await fresh.check("session.refresh", consume=True)
        if old.revision != revision:
            raise AuthError("STALE_AUTHORIZATION")
        self.authorization = fresh
        self.endpoint.connections[self.websocket] = fresh
        if self.peer is not None:
            self.peer.authorization = fresh
            self.peer.check()
        if self.observation is not None:
            await self.observation.record(
                self.observation.body["type"],
                self.observation.body["snapshot"],
                self.observation.body["data"],
                epoch=claims["authorization_epoch"],
            )

    async def _worker(self) -> None:
        while True:
            request, size, arrived = await self.requests.get()
            self.request_bytes -= size
            op = request["op"]
            metric = self.metrics.operations[op]
            metric.count += 1
            result: dict[str, Any]
            try:
                remaining = self.REQUEST_SECONDS - (time.monotonic() - arrived)
                if remaining <= 0:
                    raise OperationError("TIMEOUT")
                try:
                    async with asyncio.timeout(remaining):
                        await self.endpoint.operations.acquire()
                except TimeoutError:
                    raise OperationError("TIMEOUT") from None
                try:
                    metric.queue_seconds += time.monotonic() - arrived
                    self.metrics.active += 1
                    self.metrics.peak_active = max(self.metrics.peak_active, self.metrics.active)
                    if op == "session.refresh":
                        await self._refresh(request)
                        result = {}
                    elif op == "session.ping":
                        if self.peer is not None:
                            await self.peer.authorize(op, request["payload"])
                        else:
                            await self.authorization.check(op)
                        result = {}
                    elif op.startswith("media."):
                        # Authority preflight is separate: SDP never visits Django/Broka.
                        if self.peer is None:
                            await self.authorization.authorize(op, request["payload"])
                            raise OperationError("UNAVAILABLE")
                        await self.peer.authorize(op, request["payload"])
                        result = await self.peer.dispatch(op, request["payload"])
                        if op != "media.trickle":
                            await self.observe_media()
                    else:
                        raise AuthError("FORBIDDEN")
                    self._reply(request, result)
                finally:
                    self.metrics.active -= 1
                    self.endpoint.operations.release()
            except OperationError as exc:
                metric.errors += 1
                self._reply(request, error=exc.code)
                if exc.code == "OUTCOME_UNKNOWN":
                    async with asyncio.timeout(2):
                        await self.replies.join()
                    raise CloseSession(1012) from None
            except BaseException:
                metric.errors += 1
                raise
            finally:
                elapsed = time.monotonic() - arrived
                metric.total_seconds += elapsed
                metric.maximum_seconds = max(metric.maximum_seconds, elapsed)
                self.requests.task_done()

    async def run(self, hello: dict[str, Any]) -> None:
        tasks: list[asyncio.Task[Any]] = []
        self.records[hello["id"]] = Record(self._digest(hello))
        try:
            result: dict[str, Any] = {}
            if (
                self.endpoint.media is not None
                and "lobby" not in self.authorization.claims["scopes"]
            ):
                self.peer = self.endpoint.media.open(self.authorization, self.notify)
                self.peer.observe = self.observe_media
                result = {
                    "participant_id": self.peer.participant,
                    "scopes": self.authorization.claims["scopes"],
                    "authorization_epoch": self.authorization.claims["authorization_epoch"],
                    "gateway_owner_id": self.peer.runtime.owner_id,
                    "gateway_generation": self.peer.generation,
                }
            runtime = self.endpoint.event_runtime
            if runtime is not None:
                lease = runtime.leases.get(self.authorization.claims["meeting_id"])
                if lease is None:
                    raise OwnershipError("Observation owner unavailable.")
                runtime.require(self.authorization.claims["meeting_id"], lease.generation)
                self.observation = await runtime.lifecycle.open(
                    self.authorization.claims, lease.generation, hello["id"]
                )
            self._reply(hello, result)
            tasks = [
                asyncio.create_task(coro)
                for coro in (self._reader(), self._worker(), self._writer(), self.failure.wait())
            ]
            if self.peer is not None:
                tasks.extend(
                    [
                        asyncio.create_task(self.peer.forward_events()),
                        asyncio.create_task(self.peer.guard()),
                    ]
                )
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise CloseSession(1013)
        except OwnershipError:
            self.transport_lost = True
            raise CloseSession(1012) from None
        except OperationError as exc:
            raise CloseSession(1013 if exc.code == "OVERLOADED" else 1012) from None
        finally:
            cleanup = asyncio.create_task(self._cleanup(tasks), name="signalling-session-cleanup")
            cancelled: asyncio.CancelledError | None = None
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError as exc:
                    # Repeated ASGI/AnyIO cancellation must not cancel the join
                    # or leave a Janus handle alive. Preserve the caller's cancel
                    # object/message rather than a child task's bare exception.
                    cancelled = cancelled or exc
                    if cleanup.cancelled():
                        raise
            if cancelled is not None:
                raise cancelled

    async def _cleanup(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.peer is not None:
            await self.peer.aclose()
        if self.observation is not None:
            await self.observation.close(lost=self.transport_lost)

    async def observe_media(self) -> None:
        if self.observation is None or self.peer is None:
            return
        from .lifecycle_events import snapshot

        handles = self.peer.handles
        ready = any(h.webrtc_ready for h in handles.values())
        state = snapshot(
            "MEDIA_CONNECTED" if ready else "MEDIA_CONNECTING" if handles else "SIGNALING_CONNECTED"
        )
        publisher = handles.get("publisher")
        if publisher and publisher.published and publisher.webrtc_ready:
            for track in publisher.tracks.values():
                source = track["source"]
                state[
                    "screen"
                    if source.startswith("screen_share")
                    else "audio"
                    if source == "microphone"
                    else "video"
                ] = True
        if (
            state == self.observation.body["snapshot"]
            and self.authorization.claims["authorization_epoch"]
            == self.observation.body["authorization_epoch"]
        ):
            return
        if ready:
            kind = "participant.media_publishing_changed"
            data = {k: state[k] for k in ("audio", "video", "screen")}
            if self.observation.body["snapshot"]["connection"] != "MEDIA_CONNECTED":
                kind, data = (
                    "participant.media_connected",
                    {
                        "media_kind": "audio"
                        if state["audio"]
                        else "video"
                        if state["video"]
                        else "screen"
                        if state["screen"]
                        else next(
                            (
                                stream["media_kind"]
                                for handle in handles.values()
                                if handle.webrtc_ready
                                for stream in handle.streams
                                if stream["is_active"]
                            ),
                            "data",
                        )
                    },
                )
        elif handles:
            kind, data = "participant.media_connecting", {"handle_type": next(iter(handles))}
        else:
            kind, data = "participant.media_disconnected", {"reason_code": "requested"}
        await self.observation.record(
            kind, state, data, epoch=self.authorization.claims["authorization_epoch"]
        )
