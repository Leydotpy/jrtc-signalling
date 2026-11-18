"""ASGI-owned JRTC runtime. No Django, ORM objects or persisted-handle adoption."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from jrtc import JanusSessionManager
from jrtc.auth import JanusCredentials
from jrtc.core.exceptions import JanusErrorResponse
from jrtc.messaging import JanusEventPublisher
from jrtc.messaging.factory import create_broker
from jrtc.models.request import DetachPluginRequest
from jrtc.models.response import (
    DetachedResponse,
    ErrorResponse,
    JanusResponse,
    SuccessResponse,
    TimeoutResponse,
    WebRTCUpResponse,
)
from jrtc.session.websocket import WebsocketSession
from jrtc_video import VideoRoomPlugin, VideoRoomService

from .bus import BusConfig, BusError, _read, parse
from .commands import CommandRuntime
from .multiworker import Elections, MeetingManagers, worker_routes
from .ownership import OPERATION_SECONDS, OwnerLease, OwnershipClient, OwnershipError
from .runtime import ComponentStatus, DependencyHealth

if TYPE_CHECKING:
    from .moderation import ModerationEnforcer


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
    workers: dict[str, str] | None = None

    @classmethod
    def read(cls, path: str) -> "JanusConfig":
        try:
            body = parse(_read(path))
            workers = worker_routes(body.pop("workers")) if "workers" in body else None
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
            return cls(body["janus_url"], secret, body["ownership_url"], tuple(meetings), workers)
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
        self.uncertain: dict[str, _Binding] = {}
        self.unresolved_meetings: set[str] = set()
        self.locks = {meeting: asyncio.Lock() for meeting in runtime.config.meetings}
        self.closing = False
        self._cleanup_failed = False

    @property
    def cleanup_failed(self) -> bool:
        return self._cleanup_failed or bool(self.uncertain or self.unresolved_meetings)

    @cleanup_failed.setter
    def cleanup_failed(self, value: bool) -> None:
        self._cleanup_failed = value

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
            if (
                len(self.bindings) + len(self.uncertain) >= 256
                or meeting in self.unresolved_meetings
            ):
                raise OwnershipError("Handle capacity exceeded.")
            session = self.runtime.session(meeting)
            plugin = VideoRoomPlugin(session=session, event_queue_size=64)
            attached: _Binding | None = None
            try:
                async with asyncio.timeout_at(deadline):
                    await plugin.attach()
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
                    attached = binding
                    self.runtime.require(meeting, generation)
                    if not self.live(binding):
                        raise OwnershipError("Janus ownership changed during attachment.")
                    self.bindings[ref.token] = binding
                    return ref
            except BaseException:
                # An attach whose reply was lost may have allocated an unknown
                # remote ID. A local no-op detach cannot certify its absence.
                if attached is not None:
                    with suppress(BaseException):
                        await self._dispose(attached)
                else:
                    self.unresolved_meetings.add(meeting)
                    with suppress(BaseException):
                        async with asyncio.timeout(2):
                            await plugin.detach()
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
        self.uncertain[binding.ref.token] = binding
        async with asyncio.timeout(2):
            await binding.plugin.detach()
            await self._confirm_detached(binding)

    async def _confirm_detached(self, binding: _Binding) -> None:
        # JRTC's detach also performs local cleanup when a session is lost and
        # does not validate the reply. Local absence alone cannot prove removal.
        # A replacement transport may query the old, immutable Janus IDs. It
        # must never substitute its own IDs or adopt the old handle locally.
        session = self.runtime.session(binding.ref.meeting_id)
        try:
            response = await session.send(
                DetachPluginRequest(
                    session_id=binding.ref.janus_session_id,
                    handle_id=binding.ref.janus_handle_id,
                ),
                timeout=1,
                wait_for_event=False,
            )
        except JanusErrorResponse as exc:
            if exc.code not in {458, 459}:
                raise OwnershipError("Remote handle cleanup is unconfirmed.") from None
        else:
            if not isinstance(response, SuccessResponse) and not (
                isinstance(response, ErrorResponse) and response.error.code in {458, 459}
            ):
                raise OwnershipError("Remote handle cleanup is unconfirmed.")
        await binding.plugin.aclose()
        self.uncertain.pop(binding.ref.token, None)

    async def retry_cleanup(self) -> None:
        # Bounded, fair retries; a failed handle cannot starve the rest. This
        # runs separately from ownership renewal and the Janus receive loop.
        for token, binding in tuple(self.uncertain.items())[:8]:
            async with self.locks[binding.ref.meeting_id]:
                if token not in self.uncertain:
                    continue
                try:
                    deadline = self.runtime.require(
                        binding.ref.meeting_id, binding.ref.owner_generation
                    )
                    async with asyncio.timeout_at(
                        min(deadline, asyncio.get_running_loop().time() + 2)
                    ):
                        await self._confirm_detached(binding)
                except Exception:
                    self.uncertain.pop(token, None)
                    self.uncertain[token] = binding

    async def reconcile(
        self,
        meeting: str,
        generation: int,
        *,
        participant: str | None = None,
        preserve: set[str] | None = None,
    ) -> None:
        async with self.locks[meeting]:
            self.runtime.require(meeting, generation)
            for binding in (*tuple(self.bindings.values()), *tuple(self.uncertain.values())):
                ref = binding.ref
                if preserve and ref.token in preserve:
                    continue
                if ref.meeting_id != meeting or (
                    participant is not None and ref.participant_id != participant
                ):
                    continue
                if ref.token in self.bindings:
                    await self._dispose(binding)
                else:
                    async with asyncio.timeout(2):
                        await self._confirm_detached(binding)
                self.runtime.require(meeting, generation)

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

        await self.retry_cleanup()

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
        from .lifecycle_events import LifecyclePublisher

        self.bus = bus
        self.lifecycle: LifecyclePublisher | None = None
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

        def manager() -> JanusSessionManager:
            return JanusSessionManager(
                pool_size=1, session_factory=self._new_session, event_publisher=self.publisher
            )

        self.manager = MeetingManagers(config.meetings, manager) if config.workers else manager()
        self.elections = Elections(self, config.workers) if config.workers else None
        self.retiring = False
        self.close_connections: Callable[[], Any] | None = None
        self.registry = HandleRegistry(self)
        self.services: dict[tuple[Any, int], VideoRoomService] = {}
        self.control = CommandRuntime(bus, incarnation=self.incarnation if self.elections else None)
        self.control.consumer.enforcer = self
        self.moderation: ModerationEnforcer | None = None
        self.task: asyncio.Task[None] | None = None
        self.reconciler: asyncio.Task[None] | None = None
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
        if self.elections is None:
            for meeting in self.config.meetings:
                self.leases[meeting] = await self.authority.request(meeting, "claim")
        await self.publisher.start()
        await self.manager.start()
        if self.lifecycle is not None:
            await self.lifecycle.start()
        if self.elections is not None:
            await self._campaign()
        self.active = True
        await self.control.start()
        self.task = asyncio.create_task(self._maintain(), name="gateway-janus-ownership")
        self.reconciler = asyncio.create_task(self._reconcile(), name="gateway-janus-cleanup")

    async def _campaign(self) -> None:
        elections = self.elections
        if elections is None or self.stopping.is_set() or self.retiring:
            return

        async def compete(meeting: str) -> None:
            try:
                lease = await elections.campaign(meeting)
            except Exception:
                # A standby stays available for the next election after an outage.
                # An ambiguous grant may already be durable. Retire that process.
                if meeting in self.leases:
                    raise
                return
            if lease is not None:
                self.leases[meeting] = lease

        async with asyncio.TaskGroup() as group:
            for meeting in self.config.meetings:
                if meeting not in self.leases:
                    group.create_task(compete(meeting))

    def require(self, meeting: str, generation: int) -> float:
        now = asyncio.get_running_loop().time()
        lease = self.leases.get(meeting)
        if (
            not self.active
            or self.stopping.is_set()
            or asyncio.get_running_loop() is not self.loop
            or lease is None
            or not self.manager.ready
            or (
                self.elections is not None
                and self.manager.get_session(key=meeting)
                is not self.elections.sessions.get(meeting)
            )
            or (
                self.lifecycle is not None
                and (
                    self.lifecycle.failed
                    or (self.lifecycle.task is not None and self.lifecycle.task.done())
                )
            )
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
            if self.retiring or command["target_owner"] != self.owner_id:
                raise OwnershipError("Wrong owner.")
        except OwnershipError:
            return "failed", "owner_lost"
        if self.moderation is None:
            return "failed", "unavailable"
        return await self.moderation.apply(command)

    async def _maintain(self) -> None:
        renew_at = asyncio.get_running_loop().time() + 5
        campaign_at = asyncio.get_running_loop().time() + 1
        try:
            while not self.stopping.is_set():
                if self.loss.is_set():
                    self.loss.clear()
                    if self.lifecycle is not None:
                        await self.lifecycle.lost()
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
                if not self.retiring and asyncio.get_running_loop().time() >= renew_at:
                    for meeting, lease in tuple(self.leases.items()):
                        # Django refuses renewal after meeting end. Retain the
                        # existing finite lease for cleanup/outcome delivery;
                        # never let that refusal drain other owned meetings.
                        if self.moderation is not None and meeting in self.moderation.terminated:
                            continue
                        try:
                            if self.elections is None:
                                fresh = await self.authority.request(
                                    meeting, "renew", lease.generation
                                )
                            else:
                                fresh = await self.elections.renew(meeting, lease)
                            self.leases[meeting] = fresh
                        except OwnershipError:
                            if self.moderation is None or meeting not in self.moderation.terminated:
                                raise
                    renew_at = asyncio.get_running_loop().time() + 5
                if asyncio.get_running_loop().time() >= campaign_at:
                    await self._campaign()
                    campaign_at = asyncio.get_running_loop().time() + 1
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

    async def _reconcile(self) -> None:
        try:
            while not self.stopping.is_set():
                await self.registry.prune()
                if self.moderation is not None:
                    await self.moderation.reconcile()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), 0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.active = False
            await self.drain()
            with suppress(Exception):
                await self.aclose()

    async def begin_rolling_drain(self) -> None:
        if self.elections is not None:
            self.retiring = True
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await self.elections.withdraw(self.leases)

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
        tasks = [t for t in (self.task, self.reconciler) if t is not None and t is not caller]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clean = True

        async def attempt(operation: Any, budget: float = 3) -> None:
            nonlocal clean
            try:
                async with asyncio.timeout(budget):
                    await operation
            except Exception:
                clean = False

        if self.elections is not None:
            if self.close_connections is not None:
                await attempt(self.close_connections(), 5)
            await attempt(self.elections.withdraw(self.leases))
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
        if self.lifecycle is not None:
            await attempt(self.lifecycle.aclose())
        if clean and not self.registry.cleanup_failed:
            await asyncio.gather(
                *(
                    attempt(self.authority.request(meeting, "release", lease.generation))
                    for meeting, lease in self.leases.items()
                )
            )
        self.leases.clear()
        await attempt(self.authority.close())
        if self.elections is not None:
            await attempt(self.elections.redis.aclose())
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
            broka=ComponentStatus.DEGRADED
            if self.lifecycle is not None
            and (
                not self.lifecycle.available
                or self.lifecycle.failed
                or (self.lifecycle.task is not None and self.lifecycle.task.done())
            )
            else self.control.status,
            authority=ComponentStatus.READY if current else ComponentStatus.UNAVAILABLE,
            ownership=(
                ComponentStatus.DEGRADED if self.registry.cleanup_failed else ComponentStatus.READY
            )
            if current
            else ComponentStatus.UNAVAILABLE,
        )
