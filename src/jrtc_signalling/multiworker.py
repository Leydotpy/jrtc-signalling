"""Redis routes and durable, reconciled elections for the optional worker pool."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jrtc import JanusSessionManager
from jrtc.core.exceptions import JanusErrorResponse
from jrtc.models.request import DestroySessionRequest, KeepAliveRequest
from jrtc.models.response import ErrorResponse, SuccessResponse
from jrtc.session.websocket import WebsocketSession
from jrtc_video import VideoRoomDestroyRequest, VideoRoomService
from redis.asyncio import Redis

from .bus import ID
from .ownership import OwnerLease, OwnershipBusy, OwnershipError

if TYPE_CHECKING:
    from .janus_runtime import JanusRuntime

ROUTE_TTL = 15
_SWAP = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if ARGV[2] == '' then return redis.call('DEL', KEYS[1]) end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""


def worker_routes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise ValueError("Invalid worker routes.")
    for owner, target in value.items():
        if not isinstance(owner, str) or not ID.fullmatch(owner) or not isinstance(target, str):
            raise ValueError("Invalid worker route.")
        url = urlsplit(target)
        if (
            url.scheme != "wss"
            or not url.hostname
            or not url.path.startswith("/")
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or len(target) > 2048
            or any(ord(ch) <= 32 for ch in target)
        ):
            raise ValueError("Invalid worker URL.")
        _ = url.port
    if len(set(value.values())) != len(value):
        raise ValueError("Each worker needs a distinct direct URL.")
    return dict(value)


class MeetingManagers:
    """A dedicated SDK session per meeting makes targeted remote cleanup safe."""

    def __init__(
        self, meetings: tuple[str, ...], factory: Callable[[], JanusSessionManager]
    ) -> None:
        self.managers = {meeting: factory() for meeting in meetings}

    @property
    def ready(self) -> bool:
        return all(manager.ready for manager in self.managers.values())

    @property
    def sessions(self) -> tuple[WebsocketSession, ...]:
        return tuple(s for manager in self.managers.values() for s in manager.sessions)

    def get_session(self, *, key: str) -> WebsocketSession | None:
        return self.managers[key].get_session()

    async def start(self) -> None:
        async with asyncio.TaskGroup() as group:
            for manager in self.managers.values():
                group.create_task(manager.start())

    async def stop(self) -> None:
        await asyncio.gather(*(manager.stop() for manager in self.managers.values()))


@dataclass(frozen=True, slots=True)
class Route:
    owner: str
    incarnation: str
    generation: int

    def encode(self) -> str:
        return f"{self.owner}/{self.incarnation}/{self.generation}"


class Elections:
    def __init__(self, runtime: JanusRuntime, routes: dict[str, str]) -> None:
        if runtime.owner_id not in routes:
            raise ValueError("A direct URL is required for this worker.")
        self.runtime = runtime
        self.routes = routes
        config = runtime.config
        self.pool = hmac.new(
            config.api_secret.encode(), b"sg15-pool\0" + config.url.encode(), hashlib.sha256
        ).hexdigest()
        self.redis = Redis.from_url(
            runtime.bus.redis_url, decode_responses=True, socket_timeout=1, socket_connect_timeout=1
        )
        self.sessions: dict[str, WebsocketSession] = {}

    def key(self, meeting: str) -> str:
        return f"synq:gateway:owner:v1:{meeting}"

    def room(self, meeting: str, generation: int) -> tuple[int, str, str]:
        def derive(label: str) -> bytes:
            return hmac.digest(
                self.runtime.config.api_secret.encode(),
                f"sg15-room/{self.pool}/{meeting}/{generation}/{label}".encode(),
                "sha256",
            )

        return (
            int.from_bytes(derive("id")[:8], "big") % (2**63 - 1) + 1,
            derive("pin").hex(),
            derive("secret").hex(),
        )

    def route(self, lease: OwnerLease) -> Route:
        return Route(lease.owner_id, lease.incarnation, lease.generation)

    async def swap(self, meeting: str, old: str, new: str) -> bool:
        async with asyncio.timeout(1.5):
            return bool(
                await self.redis.eval(_SWAP, 1, self.key(meeting), old, new, ROUTE_TTL * 1000)
            )

    async def lookup(self, meeting: str) -> Route:
        async with asyncio.timeout(1.5):
            value = await self.redis.get(self.key(meeting))
        if isinstance(value, str):
            parts = value.split("/")
            if (
                len(parts) == 3
                and parts[0] in self.routes
                and parts[2].isdecimal()
                and len(parts[2]) <= 16
            ):
                generation = int(parts[2])
                if 0 < generation < 2**53:
                    return Route(parts[0], parts[1], generation)
        raise OwnershipError("No active worker route.")

    async def _clean_previous(
        self, session: WebsocketSession, meeting: str, generation: int, previous: int
    ) -> None:
        # The authority recorded this dedicated session before it could do work.
        # Kill its ability to send delayed Janus requests, then remove its room.
        if session.id == previous:
            raise OwnershipError("Conflicting Janus session identity.")
        try:
            reply = await session.send(DestroySessionRequest(session_id=previous), timeout=2)
        except JanusErrorResponse as exc:
            if exc.code != 458:
                raise OwnershipError("Janus session disposal is unconfirmed.") from None
        else:
            if not isinstance(reply, SuccessResponse) and not (
                isinstance(reply, ErrorResponse) and reply.error.code == 458
            ):
                raise OwnershipError("Janus session disposal is unconfirmed.")
        # Some transports acknowledge destroy before remote disposal finishes.
        # A former owner's exact session must be unusable before promotion.
        try:
            reply = await session.send(KeepAliveRequest(session_id=previous), timeout=2)
        except JanusErrorResponse as exc:
            if exc.code != 458:
                raise OwnershipError("Previous Janus session remains unconfirmed.") from None
        else:
            if not isinstance(reply, ErrorResponse) or reply.error.code != 458:
                raise OwnershipError("Previous Janus session is still reachable.")
        room, _, secret = self.room(meeting, generation)
        service = VideoRoomService(session, close_timeout=2)
        try:
            exists = await service.management_command("exists", args=(room,), kwargs={})
            if exists.data.exists is True:
                await service.management_command(
                    "destroy",
                    args=(VideoRoomDestroyRequest(room=room, secret=secret, permanent=False),),
                    kwargs={},
                )
                exists = await service.management_command("exists", args=(room,), kwargs={})
            if exists.data.exists is not False:
                raise OwnershipError("Janus room disposal is unconfirmed.")
        finally:
            await service.aclose(graceful=True)

    async def campaign(self, meeting: str) -> OwnerLease | None:
        runtime = self.runtime
        token = f"candidate/{runtime.incarnation}"
        async with asyncio.timeout(1.5):
            held = await self.redis.set(self.key(meeting), token, nx=True, ex=ROUTE_TTL)
        if not held:
            return None
        published = False
        try:
            async with asyncio.timeout(10):
                previous_generation, previous_session = await runtime.authority.inspect(
                    meeting, self.pool
                )
                session = runtime.session(meeting)
                if previous_session is not None:
                    await self._clean_previous(
                        session, meeting, previous_generation, previous_session
                    )
                if (
                    runtime.retiring
                    or runtime.stopping.is_set()
                    or runtime.session(meeting) is not session
                ):
                    raise OwnershipError("Worker or session is draining.")
                lease = await runtime.authority.request(
                    meeting, "elect", previous_generation, pool=self.pool, session_id=session.id
                )
                # Retain the SQL grant for cleanup even if Redis rejects the promotion.
                runtime.leases[meeting] = lease
                started = asyncio.get_running_loop().time()
                if runtime.retiring or runtime.stopping.is_set():
                    raise OwnershipError("Worker is draining.")
                if not await self.swap(meeting, token, self.route(lease).encode()):
                    raise OwnershipError("Election reservation expired.")
                published = True
                self.sessions[meeting] = session
                return replace(lease, deadline=min(lease.deadline, started + ROUTE_TTL - 1))
        except OwnershipBusy:
            return None
        finally:
            if not published:
                await self.swap(meeting, token, "")

    async def renew(self, meeting: str, lease: OwnerLease) -> OwnerLease:
        if self.runtime.session(meeting) is not self.sessions.get(meeting):
            raise OwnershipError("Registered Janus session was replaced.")
        route = self.route(lease).encode()
        started = asyncio.get_running_loop().time()
        if not await self.swap(meeting, route, route):
            raise OwnershipError("Redis ownership was lost.")
        fresh = await self.runtime.authority.request(meeting, "renew", lease.generation)
        return replace(fresh, deadline=min(fresh.deadline, started + ROUTE_TTL - 1))

    async def withdraw(self, leases: dict[str, OwnerLease]) -> None:
        for meeting, lease in tuple(leases.items()):
            await self.swap(meeting, self.route(lease).encode(), "")
