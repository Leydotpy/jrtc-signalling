"""ASGI-owned JRTC runtime. No Django, ORM objects or persisted-handle adoption."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from jrtc import JanusSessionManager
from jrtc.auth import JanusCredentials
from jrtc.messaging import JanusEventPublisher
from jrtc.messaging.factory import create_broker
from jrtc.models.response import DetachedResponse, JanusResponse, TimeoutResponse, WebRTCUpResponse
from jrtc.session.websocket import WebsocketSession
from jrtc_video import VideoRoomPlugin, VideoRoomService

from .bus import BusConfig, BusError, _read, parse
from .commands import CommandRuntime
from .ownership import OPERATION_SECONDS, OwnerLease, OwnershipClient, OwnershipError
from .runtime import ComponentStatus, DependencyHealth


def janus_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise OwnershipError("Invalid Janus identifier.")
    return value


@dataclass(frozen=True, repr=False)
class JanusConfig:
    url: str
    api_secret: str = field(repr=False)
    ownership_url: str
    meetings: tuple[str, ...]

    @classmethod
    def read(cls, path: str) -> "JanusConfig":
        try:
            body = parse(_read(path))
            if set(body) != {"janus_url", "api_secret_file", "ownership_url", "meetings"}:
                raise ValueError
            for key, schemes in (("janus_url", {"ws", "wss"}), ("ownership_url", {"https"})):
                url = urlsplit(body[key])
                if (
                    url.scheme not in schemes
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError
                if url.scheme == "ws" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                    raise ValueError
            meetings = body["meetings"]
            if not isinstance(meetings, list) or not 1 <= len(meetings) <= 16:
                raise ValueError
            if any(not isinstance(m, str) or str(UUID(m)) != m for m in meetings):
                raise ValueError
            if len(set(meetings)) != len(meetings):
                raise ValueError
            secret = _read(body["api_secret_file"], 4096).decode().strip()
            if len(secret) < 32:
                raise ValueError
            return cls(body["janus_url"], secret, body["ownership_url"], tuple(meetings))
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            raise BusError("Invalid Janus runtime configuration.") from None


class ObservationPublisher(JanusEventPublisher):
    """Retain the generic nonblocking publisher without exporting raw media.

    SG-07's bounded in-process sink is diagnostic only. SG-10 owns durable domain
    observations. Construct fixed-field copies before generic publication so no
    plugin data, JSEP, ICE, reason text or arbitrary extras enter Broka.
    """

    async def admit(self, response: JanusResponse, **kwargs: Any) -> bool:
        return self.try_admit(response, **kwargs)

    def try_admit(self, response: JanusResponse, **kwargs: Any) -> bool:
        safe: JanusResponse
        if response.janus == "webrtcup":
            safe = WebRTCUpResponse(
                janus="webrtcup", session_id=response.session_id, sender=janus_id(response.sender)
            )
        elif response.janus == "detached":
            safe = DetachedResponse(
                janus="detached", session_id=response.session_id, sender=janus_id(response.sender)
            )
        elif response.janus == "timeout":
            safe = TimeoutResponse(janus="timeout", session_id=response.session_id)
        else:
            return False
        return super().try_admit(safe, **kwargs)


@dataclass(frozen=True, slots=True)
class HandleRef:
    token: str
    meeting_id: str
    participant_id: str
    participant_session_id: str
    role: str
    owner_id: str
    owner_generation: int
    janus_session_id: int
    janus_handle_id: int

    def __post_init__(self) -> None:
        janus_id(self.janus_session_id)
        janus_id(self.janus_handle_id)
        janus_id(self.owner_generation)

    def wire_ids(self) -> dict[str, str]:
        return {
            "handle_id": self.token,
            "janus_session_id": str(self.janus_session_id),
            "janus_handle_id": str(self.janus_handle_id),
        }


@dataclass(slots=True)
class _Binding:
    ref: HandleRef
    plugin: Any
    session: Any
    generation: int


class HandleRegistry:
    def __init__(self, runtime: "JanusRuntime") -> None:
        self.runtime = runtime
        self.bindings: dict[str, _Binding] = {}
        self.locks = {meeting: asyncio.Lock() for meeting in runtime.config.meetings}
        self.closing = False
        self.cleanup_failed = False

    def live(self, binding: _Binding) -> bool:
        return (
            binding.session.ready
            and binding.session.generation == binding.generation
            and binding.session.plugins.get(binding.ref.janus_handle_id) is binding.plugin
            and self.runtime.manager.get_session(key=binding.ref.meeting_id) is binding.session
        )

    async def attach(
        self, meeting: str, participant: str, connection: str, role: str, generation: int
    ) -> HandleRef:
        if role not in {"publisher", "subscriber"} or meeting not in self.locks:
            raise OwnershipError("Invalid handle scope.")
        for value in (participant, connection):
            if str(UUID(value)) != value:
                raise OwnershipError("Invalid handle scope.")
        async with self.locks[meeting]:
            deadline = self.runtime.require(meeting, generation)
            if self.closing:
                raise OwnershipError("Handles are draining.")
            for binding in tuple(self.bindings.values()):
                ref = binding.ref
                if (ref.meeting_id, ref.participant_session_id, ref.role) == (
                    meeting,
                    connection,
                    role,
                ):
                    if ref.participant_id != participant or not self.live(binding):
                        raise OwnershipError("Handle binding is stale or conflicting.")
                    return ref
            if len(self.bindings) >= 256:
                raise OwnershipError("Handle capacity exceeded.")
            session = self.runtime.session(meeting)
            plugin = VideoRoomPlugin(session=session, event_queue_size=64)
            try:
                async with asyncio.timeout_at(deadline):
                    await plugin.attach()
                    self.runtime.require(meeting, generation)
                    ref = HandleRef(
                        str(uuid4()),
                        meeting,
                        participant,
                        connection,
                        role,
                        self.runtime.owner_id,
                        generation,
                        janus_id(session.id),
                        janus_id(plugin.id),
                    )
                    binding = _Binding(ref, plugin, session, session.generation)
                    if not self.live(binding):
                        raise OwnershipError("Janus ownership changed during attachment.")
                    self.bindings[ref.token] = binding
                    return ref
            except BaseException:
                try:
                    async with asyncio.timeout(2):
                        await plugin.detach()
                except BaseException:
                    self.cleanup_failed = True
                raise

    async def invoke(self, ref: HandleRef, operation: Callable[[Any], Any]) -> Any:
        if ref.meeting_id not in self.locks:
            raise OwnershipError("Unknown handle scope.")
        async with self.locks[ref.meeting_id]:
            deadline = self.runtime.require(ref.meeting_id, ref.owner_generation)
            binding = self.bindings.get(ref.token)
            if self.closing or binding is None or binding.ref != ref or not self.live(binding):
                raise OwnershipError("Handle binding is stale.")
            try:
                async with asyncio.timeout_at(deadline):
                    result = await operation(binding.plugin)
                    self.runtime.require(ref.meeting_id, ref.owner_generation)
                    if not self.live(binding):
                        raise OwnershipError("Janus ownership changed during operation.")
                    return result
            except BaseException:
                # Never allow an uncertain operation to be replayed on this token.
                await self._dispose(binding)
                raise

    async def _dispose(self, binding: _Binding) -> None:
        self.bindings.pop(binding.ref.token, None)
        try:
            async with asyncio.timeout(2):
                await binding.plugin.detach()
        except BaseException:
            self.cleanup_failed = True
            raise

    async def detach(self, ref: HandleRef) -> None:
        async with self.locks[ref.meeting_id]:
            binding = self.bindings.get(ref.token)
            if binding is None or binding.ref != ref:
                raise OwnershipError("Handle binding is stale.")
            self.runtime.require(ref.meeting_id, ref.owner_generation)
            await self._dispose(binding)

    async def prune(self) -> None:
        for meeting, lock in self.locks.items():
            async with lock:
                for binding in tuple(self.bindings.values()):
                    if binding.ref.meeting_id == meeting and not self.live(binding):
                        with suppress(Exception):
                            await self._dispose(binding)

    async def close(self) -> None:
        self.closing = True
        for meeting, lock in self.locks.items():
            async with lock:
                for binding in tuple(self.bindings.values()):
                    if binding.ref.meeting_id == meeting:
                        with suppress(Exception):
                            await self._dispose(binding)


class JanusRuntime:
    def __init__(self, bus: BusConfig, config: JanusConfig) -> None:
        self.config, self.owner_id = config, bus.signer.producer
        self.incarnation = str(uuid4())
        self.authority = OwnershipClient(bus, config.ownership_url, self.incarnation)
        self.leases: dict[str, OwnerLease] = {}
        self.active = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loss = asyncio.Event()
        self.observers: dict[Any, Callable[[], None]] = {}
        self.publisher = ObservationPublisher(
            create_broker(engine="memory", physical_route="gateway.local.observations"),
            workers=1,
            queue_capacity=128,
            publish_timeout=0.5,
        )
        self.manager = JanusSessionManager(
            pool_size=1, session_factory=self._new_session, event_publisher=self.publisher
        )
        self.registry = HandleRegistry(self)
        self.services: dict[tuple[Any, int], VideoRoomService] = {}
        self.control = CommandRuntime(bus)
        self.control.consumer.enforcer = self
        self.task: asyncio.Task[None] | None = None
        self.stopping = asyncio.Event()
        self.cleanup: asyncio.Task[None] | None = None

    def _new_session(self) -> WebsocketSession:
        session = WebsocketSession(
            url=self.config.url,
            credentials=JanusCredentials(api_secret=self.config.api_secret),
            request_timeout=OPERATION_SECONDS,
            keepalive_interval=5,
            keepalive_failures=1,
            shutdown_timeout=3,
            detach_concurrency=4,
            event_publisher=self.publisher,
        )
        # Manager retains set_loss_handler. The observer performs no I/O and
        # coalesces arbitrary bursts into a single owned reconciliation worker.
        self.observers[session] = session.add_loss_observer(lambda _loss: self.loss.set())
        return session

    async def start(self) -> None:
        if self.loop is not None:
            raise OwnershipError("Runtime cannot be started twice.")
        self.loop = asyncio.get_running_loop()
        for meeting in self.config.meetings:
            self.leases[meeting] = await self.authority.request(meeting, "claim")
        await self.publisher.start()
        await self.manager.start()
        self.active = True
        await self.control.start()
        self.task = asyncio.create_task(self._maintain(), name="gateway-janus-ownership")

    def require(self, meeting: str, generation: int) -> float:
        now = asyncio.get_running_loop().time()
        lease = self.leases.get(meeting)
        if (
            not self.active
            or self.stopping.is_set()
            or asyncio.get_running_loop() is not self.loop
            or lease is None
            or not self.manager.ready
        ):
            raise OwnershipError("Runtime ownership is unavailable.")
        lease.require(self.owner_id, generation, now=now)
        if lease.deadline - now < 0.25:
            raise OwnershipError("Ownership lease is expiring.")
        return min(now + OPERATION_SECONDS, lease.deadline)

    def session(self, meeting: str) -> Any:
        session = self.manager.get_session(key=meeting)
        if session is None or not session.ready:
            raise OwnershipError("Janus session is unavailable.")
        return session

    async def management(
        self, meeting: str, generation: int, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        # Internal adapter, never a client-controlled arbitrary method dispatcher.
        if method not in {"exists", "list_rooms", "create", "destroy", "list_participants"}:
            raise OwnershipError("Unsupported management operation.")
        async with self.registry.locks[meeting]:
            deadline = self.require(meeting, generation)
            session = self.session(meeting)
            key = (session, session.generation)
            service = self.services.get(key)
            if service is None:
                service = VideoRoomService(session, close_timeout=2)
                self.services[key] = service
            async with asyncio.timeout_at(deadline):
                result = await service.management_command(method, args=args, kwargs=kwargs)
                self.require(meeting, generation)
                if self.session(meeting) is not session or not session.ready:
                    raise OwnershipError("Janus session was replaced.")
                return result

    async def apply(self, command: dict[str, Any]) -> tuple[str, str]:
        try:
            self.require(command["meeting_id"], command["expected_owner_generation"])
            if command["target_owner"] != self.owner_id:
                raise OwnershipError("Wrong owner.")
        except OwnershipError:
            return "failed", "owner_lost"
        # Physical moderation/admission is SG-13. A routed request and a live
        # Janus connection alone are not evidence that permission was enforced.
        return "failed", "unavailable"

    async def _maintain(self) -> None:
        renew_at = asyncio.get_running_loop().time() + 5
        try:
            while not self.stopping.is_set():
                await self.registry.prune()
                for key, service in tuple(self.services.items()):
                    if not service.session.ready or key != (
                        service.session,
                        service.session.generation,
                    ):
                        await service.aclose(graceful=False)
                        self.services.pop(key, None)
                for session, unsubscribe in tuple(self.observers.items()):
                    if session not in self.manager.sessions and session.state.value in {
                        "closed",
                        "lost",
                    }:
                        unsubscribe()
                        self.observers.pop(session, None)
                if asyncio.get_running_loop().time() >= renew_at:
                    for meeting, lease in tuple(self.leases.items()):
                        self.leases[meeting] = await self.authority.request(
                            meeting, "renew", lease.generation
                        )
                    renew_at = asyncio.get_running_loop().time() + 5
                self.loss.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.loss.wait(), 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.active = False
            # Never reacquire an expired/uncertain generation in this process.
            await self.drain()
            with suppress(Exception):
                await self.aclose()

    async def drain(self) -> None:
        self.active = False
        self.stopping.set()
        self.registry.closing = True
        await self.control.drain()

    async def aclose(self) -> None:
        if self.cleanup is None:
            # Exclude the maintenance caller from cancellation/join to avoid
            # awaiting a task that itself waits on this shared cleanup.
            caller = asyncio.current_task()
            self.cleanup = asyncio.create_task(self._close(caller), name="gateway-janus-close")
        await asyncio.shield(self.cleanup)

    async def _close(self, caller: asyncio.Task[Any] | None) -> None:
        await self.drain()
        if self.task is not None and self.task is not caller:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        clean = True

        async def attempt(operation: Any, budget: float = 3) -> None:
            nonlocal clean
            try:
                async with asyncio.timeout(budget):
                    await operation
            except Exception:
                clean = False

        await attempt(self.control.aclose())
        await attempt(self.registry.close())
        await asyncio.gather(
            *(attempt(service.aclose(graceful=True)) for service in self.services.values())
        )
        self.services.clear()
        for unsubscribe in self.observers.values():
            unsubscribe()
        self.observers.clear()
        # Explicitly await destruction: manager.stop() suppresses per-session
        # destroy errors, which must not turn uncertain cleanup into a release.
        await asyncio.gather(*(attempt(session.destroy()) for session in self.manager.sessions))
        await attempt(self.manager.stop())
        self.registry.bindings.clear()
        await attempt(self.publisher.stop(drain=True, timeout=2))
        if clean and not self.registry.cleanup_failed:
            await asyncio.gather(
                *(
                    attempt(self.authority.request(meeting, "release", lease.generation))
                    for meeting, lease in self.leases.items()
                )
            )
        self.leases.clear()
        await attempt(self.authority.close())
        if not clean or self.registry.cleanup_failed:
            raise OwnershipError("Janus cleanup requires reconciliation.")

    def health(self) -> DependencyHealth:
        current = (
            self.active
            and bool(self.leases)
            and all(
                lease.deadline > asyncio.get_running_loop().time() for lease in self.leases.values()
            )
        )
        return DependencyHealth(
            janus=ComponentStatus.READY if self.manager.ready else ComponentStatus.UNAVAILABLE,
            broka=self.control.status,
            authority=ComponentStatus.READY if current else ComponentStatus.UNAVAILABLE,
            ownership=ComponentStatus.READY if current else ComponentStatus.UNAVAILABLE,
        )
