"""Owner-fenced VideoRoom operations for explicitly enabled media rehearsals."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from jrtc.models.base import Jsep
from jrtc_video import (
    PublisherConfigureRequest,
    PublisherJoinAndConfigureRequest,
    PublisherJoinRequest,
    PublisherStreamControl,
    StreamDescription,
    SubscriberJoinRequest,
    SubscriberUpdateRequest,
    SubscribeTarget,
    UnsubscribeTarget,
    VideoRoomCreateRequest,
    VideoRoomDestroyRequest,
    parse_videoroom_response,
)

from .auth import AuthError, AuthorizationSession
from .contracts import OperationError
from .janus_runtime import HandleRef, JanusRuntime, janus_id
from .ownership import OwnershipError
from .sdp import SOURCES, Section, publisher_tracks, sections, subscriber_answer


@dataclass(repr=False)
class Room:
    id: int
    pin: str
    secret: str
    session: Any
    generation: int
    ready: bool = False


@dataclass(repr=False)
class MediaHandle:
    ref: HandleRef
    plugin: Any = None
    joined: bool = False
    published: bool = False
    webrtc_ready: bool = False
    expected_hangup: bool = False
    feed: int | None = None
    tracks: dict[str, dict[str, str]] = field(default_factory=dict)
    selected: dict[tuple[int, str], str] = field(default_factory=dict)
    streams: list[dict[str, Any]] = field(default_factory=list)
    negotiation: str | None = None
    pending_offer: list[Section] | None = None
    seen_jsep: deque[tuple[str | None, str, str]] = field(default_factory=lambda: deque(maxlen=64))


def jsep_key(transaction: str | None, jsep: dict[str, str]) -> tuple[str | None, str, str]:
    return transaction, jsep["type"], hashlib.sha256(jsep["sdp"].encode()).hexdigest()


class MediaHub:
    def __init__(self, runtime: JanusRuntime) -> None:
        self.runtime = runtime
        self.peers: dict[tuple[str, str], MediaPeer] = {}
        self.rooms: dict[str, Room] = {}
        self.room_locks = {m: asyncio.Lock() for m in runtime.config.meetings}
        self.closing = False

    def open(
        self, authorization: AuthorizationSession, emit: Callable[[str, dict[str, Any]], None]
    ) -> MediaPeer:
        claims = authorization.claims
        key = claims["meeting_id"], claims["participant_id"]
        lease = self.runtime.leases.get(key[0])
        if self.closing or lease is None or len(self.peers) >= 128:
            raise AuthError("UNAVAILABLE")
        if key in self.peers:
            # Reconnect takeover and inherited handles are explicitly SG-14/15.
            raise AuthError("STALE_GENERATION")
        self.runtime.require(key[0], lease.generation)
        peer = MediaPeer(self, authorization, lease.generation, emit)
        self.peers[key] = peer
        return peer

    async def room(self, peer: MediaPeer) -> Room:
        async with self.room_locks[peer.meeting]:
            peer.check()
            session = self.runtime.session(peer.meeting)
            room = self.rooms.get(peer.meeting)
            if room is not None:
                if not room.ready:
                    raise OwnershipError("Room creation requires reconciliation.")
                if room.session is session and room.generation == session.generation:
                    return room
                reply = await self.runtime.management(
                    peer.meeting, peer.generation, "exists", room.id
                )
                if getattr(reply.data, "exists", False) is True:
                    room.session, room.generation = session, session.generation
                    return room
            room = Room(
                secrets.randbelow(2**63 - 1) + 1,
                secrets.token_hex(32),
                secrets.token_hex(32),
                session,
                session.generation,
            )
            # Retain the chosen ID/secret even if the create ACK is lost, for cleanup.
            self.rooms[peer.meeting] = room
            try:
                await self.runtime.management(
                    peer.meeting,
                    peer.generation,
                    "create",
                    VideoRoomCreateRequest(
                        room=room.id,
                        pin=room.pin,
                        secret=room.secret,
                        is_private=True,
                        permanent=False,
                        publishers=32,
                        record=False,
                    ),
                )
            except BaseException:
                self.runtime.registry.cleanup_failed = True
                raise
            room.ready = True
            return room

    def targets(self, peer: MediaPeer) -> dict[tuple[int, str], str]:
        targets: dict[tuple[int, str], str] = {}
        for other in self.peers.values():
            if other is peer or other.meeting != peer.meeting or other.closed:
                continue
            try:
                other.check()
            except (AuthError, OwnershipError):
                continue
            handle = other.handles.get("publisher")
            if handle and handle.published and handle.webrtc_ready and handle.feed is not None:
                for mid in handle.tracks:
                    targets[handle.feed, mid] = other.participant
        if len(targets) > 256:
            raise OperationError("OVERLOADED")
        return targets

    def topology(self, meeting: str) -> None:
        for peer in tuple(self.peers.values()):
            if peer.meeting == meeting and not peer.closed:
                for handle in peer.handles.values():
                    peer.emit(
                        "media.event",
                        {
                            "handle_type": handle.ref.role,
                            "kind": "topology_changed",
                            "janus_handle_id": str(handle.ref.janus_handle_id),
                        },
                    )

    async def aclose(self) -> None:
        self.closing = True
        await asyncio.gather(*(p.aclose() for p in tuple(self.peers.values())))
        for meeting, room in self.rooms.items():
            lease = self.runtime.leases.get(meeting)
            if lease is None:
                self.runtime.registry.cleanup_failed = True
                continue
            try:
                exists = await self.runtime.management(meeting, lease.generation, "exists", room.id)
                if exists.data.exists:
                    await self.runtime.management(
                        meeting,
                        lease.generation,
                        "destroy",
                        VideoRoomDestroyRequest(room=room.id, secret=room.secret, permanent=False),
                    )
            except BaseException as exc:
                self.runtime.registry.cleanup_failed = True
                if isinstance(exc, asyncio.CancelledError):
                    raise
        self.rooms.clear()


class MediaPeer:
    def __init__(
        self,
        hub: MediaHub,
        authorization: AuthorizationSession,
        generation: int,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self.hub, self.runtime, self.authorization = hub, hub.runtime, authorization
        self.meeting, self.participant, self.connection = (
            authorization.claims[k]
            for k in ("meeting_id", "participant_id", "participant_session_id")
        )
        self.generation, self.emit = generation, emit
        self.handles: dict[str, MediaHandle] = {}
        self.events: asyncio.Queue[tuple[MediaHandle, Any, bool]] = asyncio.Queue(maxsize=32)
        self.lock = asyncio.Lock()
        self.closed = False
        self.overflow = False
        self.used_negotiations: set[str] = set()

    def check(self) -> None:
        auth = self.authorization
        if self.closed or auth.revoked or time.time() >= auth.claims["exp"]:
            raise AuthError("STALE_AUTHORIZATION")
        self.runtime.require(self.meeting, self.generation)
        for role, handle in self.handles.items():
            binding = self.runtime.registry.bindings.get(handle.ref.token)
            if (
                binding is None
                or binding.ref != handle.ref
                or not self.runtime.registry.live(binding)
            ):
                raise OwnershipError("Media handle was replaced.")
            scope = "media:publish" if role == "publisher" else "media:subscribe"
            if scope not in auth.claims["scopes"]:
                raise AuthError("FORBIDDEN")
            if handle.published and (
                auth.grant is None
                or any(
                    not auth.grant.permissions[SOURCES[t["source"]][1]]
                    for t in handle.tracks.values()
                )
            ):
                raise AuthError("FORBIDDEN")
            if handle.plugin is not None and handle.plugin.dropped_events:
                raise OperationError("OVERLOADED")
        if self.overflow:
            raise OperationError("OVERLOADED")

    async def guard(self) -> None:
        while True:
            async with self.lock:
                self.check()
            await asyncio.sleep(0.25)

    async def authorize(self, op: str, payload: dict[str, Any]) -> None:
        # A fresh check temporarily clears the grant. Event/health processing
        # waits for its bounded result instead of treating that interval as a revoke.
        async with self.lock:
            await self.authorization.authorize(op, payload)
            self.check()

    def _event(self, handle: MediaHandle, event: Any) -> None:
        if self.closed:
            return
        expected_hangup = getattr(event, "janus", None) == "hangup" and handle.expected_hangup
        if expected_hangup:
            handle.expected_hangup = False
        try:
            self.events.put_nowait((handle, event, expected_hangup))
        except asyncio.QueueFull:
            self.overflow = True

    async def _attach(self, role: str) -> MediaHandle:
        if role in self.handles:
            return self.handles[role]
        ref = await self.runtime.registry.attach(
            self.meeting, self.participant, self.connection, role, self.generation
        )
        handle = MediaHandle(ref)
        self.handles[role] = handle

        async def listen(plugin: Any) -> None:
            handle.plugin = plugin
            await plugin.on("event", lambda event: self._event(handle, event))

        await self.runtime.registry.invoke(ref, listen)
        return handle

    async def _call(self, handle: MediaHandle, method: str, *args: Any, **kwargs: Any) -> Any:
        revision = self.authorization.revision
        result = await self.runtime.registry.invoke(
            handle.ref, lambda plugin: getattr(plugin, method)(*args, **kwargs)
        )
        self.check()
        if self.authorization.revision != revision:
            raise AuthError("STALE_AUTHORIZATION")
        return result

    @staticmethod
    def _jsep(reply: Any) -> dict[str, str] | None:
        jsep = getattr(reply, "jsep", None)
        if jsep is None:
            return None
        return {"type": jsep.type, "sdp": jsep.sdp}

    def _ack(
        self, handle: MediaHandle, action: str, reply: Any = None, *, detached: bool = False
    ) -> dict[str, Any]:
        jsep = self._jsep(reply)
        if jsep is not None:
            handle.seen_jsep.append(jsep_key(getattr(reply, "transaction", None), jsep))
        result: dict[str, Any] = {
            "action": action,
            "participant_id": self.participant,
            "handle_type": handle.ref.role,
            "lifecycle_state": "detached" if detached else "ready" if handle.joined else "attached",
            "jsep": jsep,
            "streams": handle.streams,
            "selected_streams": [
                {
                    "feed": str(feed),
                    "mid": mid,
                    "crossrefid": None,
                    "sub_mid": next(
                        (
                            s["janus_mid"]
                            for s in handle.streams
                            if s["janus_feed_id"] == str(feed) and s["janus_feed_mid"] == mid
                        ),
                        None,
                    ),
                }
                for feed, mid in handle.selected
            ],
        }
        if handle.negotiation is not None:
            result["negotiation_id"] = handle.negotiation
        return result

    def _stream(
        self,
        handle: MediaHandle,
        mid: str,
        kind: str,
        feed: int,
        feed_mid: str | None,
        source: str | None,
        *,
        active: bool = True,
    ) -> dict[str, Any]:
        return {
            "id": str(uuid5(NAMESPACE_URL, handle.ref.token + ":" + mid)),
            "direction": "outbound" if handle.ref.role == "publisher" else "inbound",
            "media_kind": kind,
            "janus_mid": mid,
            "janus_feed_id": str(feed),
            "janus_feed_mid": feed_mid,
            "is_active": active,
            "is_ready": active and handle.webrtc_ready,
            "is_moderated": False,
            "metadata": {},
            "source_participant_id": source,
        }

    @staticmethod
    def _server_sections(sdp: str) -> list[Section]:
        try:
            return sections(sdp)
        except OperationError:
            # Janus may have already applied the mutation. Do not allow a retry
            # on a handle whose negotiated state cannot be verified.
            raise OperationError("OUTCOME_UNKNOWN") from None

    def _publisher_answer(self, sdp: str, tracks: dict[str, dict[str, str]]) -> dict[str, Section]:
        accepted = {
            s.mid: s for s in self._server_sections(sdp) if s.active and s.direction != "inactive"
        }
        if any(
            mid not in tracks or s.kind != tracks[mid]["kind"] or s.direction != "recvonly"
            for mid, s in accepted.items()
        ):
            raise OperationError("OUTCOME_UNKNOWN")
        return accepted

    def _subscriber_offer(self, handle: MediaHandle, sdp: str) -> None:
        offer = self._server_sections(sdp)
        streams = {s["janus_mid"]: s for s in handle.streams}
        for section in offer:
            if not section.active or section.direction == "inactive":
                continue
            stream = streams.get(section.mid)
            if (
                stream is None
                or not stream["is_active"]
                or section.kind != stream["media_kind"]
                or section.direction != "sendonly"
            ):
                raise OperationError("OUTCOME_UNKNOWN")
        handle.pending_offer, handle.negotiation = offer, str(uuid4())

    async def dispatch(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            self.check()
            if op in {"media.trickle", "media.ice_restart"}:
                raise OperationError("NOT_IMPLEMENTED")  # Ordered ICE matrix is SG-09.
            try:
                if op.startswith("media.publisher."):
                    return await self._publisher(op, payload)
                if op in {
                    "media.subscriber.join",
                    "media.subscriber.sync",
                    "media.subscriber.update",
                }:
                    return await self._subscribe()
                if op == "media.subscriber.start":
                    return await self._start(payload)
                if op in {"media.subscriber.detach", "media.hangup"}:
                    role = payload.get("handle_type", "subscriber")
                    handle = self.handles.get(role)
                    if handle is None:
                        raise OperationError()
                    await self.runtime.registry.detach(handle.ref)
                    del self.handles[role]
                    self.hub.topology(self.meeting)
                    return self._ack(
                        handle, "detach" if op.endswith("detach") else "hangup", detached=True
                    )
                raise OperationError("NOT_IMPLEMENTED")
            except (OperationError, AuthError, asyncio.CancelledError):
                raise
            except Exception:
                # SDK errors/timeouts may follow a send: never invite mutation replay.
                raise OperationError("OUTCOME_UNKNOWN") from None

    async def _publisher(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        handle = self.handles.get("publisher")
        if op == "media.publisher.unpublish":
            if handle is None or not handle.joined:
                raise OperationError()
            if not handle.published:
                return self._ack(handle, "noop")
            handle.expected_hangup = True
            reply = await self._call(handle, "unpublish")
            handle.published, handle.tracks, handle.streams, handle.negotiation = (
                False,
                {},
                [],
                None,
            )
            handle.webrtc_ready = False
            self.hub.topology(self.meeting)
            return self._ack(handle, "unpublish", reply)
        mapped: dict[str, dict[str, str]] = {}
        if op != "media.publisher.join":
            auth = self.authorization
            if auth.grant is None:
                raise AuthError("FORBIDDEN")
            mapped = publisher_tracks(
                payload["offer"]["sdp"], payload["tracks"], auth.grant.permissions
            )
            if payload["negotiation_id"] in self.used_negotiations:
                raise OperationError("STALE_NEGOTIATION")
            if len(self.used_negotiations) >= 64:
                raise OperationError("OVERLOADED")
            if op.endswith("configure") and (handle is None or not handle.published):
                raise OperationError()
        room = await self.hub.room(self)
        handle = await self._attach("publisher")
        if op == "media.publisher.join":
            if handle.joined:
                return self._ack(handle, "noop")
            reply = await self._call(
                handle, "join_publisher", PublisherJoinRequest(room=room.id, pin=room.pin)
            )
            handle.feed, handle.joined = janus_id(reply.data.id), True
            return self._ack(handle, "join", reply)
        self.used_negotiations.add(payload["negotiation_id"])
        offer = Jsep(**payload["offer"])
        descriptions = [
            StreamDescription(mid=mid, description=t["source"]) for mid, t in mapped.items()
        ]
        if not handle.joined:
            action = "join_and_configure"
            reply = await self._call(
                handle,
                "join_and_configure",
                PublisherJoinAndConfigureRequest(
                    room=room.id, pin=room.pin, descriptions=descriptions, record=False
                ),
                offer,
            )
            handle.feed, handle.joined = janus_id(reply.data.id), True
        elif not handle.published:
            action = "publish"
            from jrtc_video import PublisherPublishRequest

            reply = await self._call(
                handle,
                "publish",
                offer,
                body=PublisherPublishRequest(descriptions=descriptions, record=False),
            )
        else:
            action = "configure"
            controls = [
                PublisherStreamControl(mid=s.mid, send=s.mid in mapped) for s in sections(offer.sdp)
            ]
            reply = await self._call(
                handle,
                "configure_publisher",
                PublisherConfigureRequest(
                    descriptions=descriptions, streams=controls, record=False
                ),
                offer=offer,
            )
        answer = self._jsep(reply)
        if answer is None or answer["type"] != "answer" or handle.feed is None:
            raise OperationError("OUTCOME_UNKNOWN")
        accepted = self._publisher_answer(answer["sdp"], mapped)
        handle.tracks = {mid: mapped[mid] for mid in accepted}
        handle.streams = [
            self._stream(
                handle,
                mid,
                "screen" if t["source"] == "screen_share" else t["kind"],
                handle.feed,
                mid,
                self.participant,
            )
            for mid, t in handle.tracks.items()
        ]
        handle.published, handle.negotiation = True, payload["negotiation_id"]
        self.hub.topology(self.meeting)
        return self._ack(handle, action, reply)

    async def _subscribe(self) -> dict[str, Any]:
        targets = self.hub.targets(self)
        handle = self.handles.get("subscriber")
        if handle is not None and handle.pending_offer is not None:
            raise OperationError("STALE_NEGOTIATION")
        if not targets and handle is None:
            return {
                "action": "noop",
                "participant_id": self.participant,
                "handle_type": "subscriber",
                "lifecycle_state": "detached",
                "streams": [],
                "selected_streams": [],
            }
        if handle is not None and targets == handle.selected:
            return self._ack(handle, "noop")
        room = await self.hub.room(self)
        handle = await self._attach("subscriber")
        added, removed = (
            targets.keys() - handle.selected.keys(),
            handle.selected.keys() - targets.keys(),
        )
        if not handle.joined:
            action = "join"
            reply = await self._call(
                handle,
                "join_subscriber",
                SubscriberJoinRequest(
                    room=room.id,
                    pin=room.pin,
                    autoupdate=False,
                    streams=[SubscribeTarget(feed=f, mid=m) for f, m in targets],
                ),
            )
            handle.joined = True
        else:
            action = "update"
            reply = await self._call(
                handle,
                "update_subscription",
                SubscriberUpdateRequest(
                    subscribe=[SubscribeTarget(feed=f, mid=m) for f, m in added] or None,
                    unsubscribe=[UnsubscribeTarget(feed=f, mid=m) for f, m in removed] or None,
                ),
            )
        handle.selected = targets
        self._subscriber_layout(handle, reply)
        jsep = self._jsep(reply)
        if jsep is not None:
            if jsep["type"] != "offer":
                raise OperationError("OUTCOME_UNKNOWN")
            self._subscriber_offer(handle, jsep["sdp"])
        elif action == "join":
            raise OperationError("OUTCOME_UNKNOWN")
        return self._ack(handle, action, reply)

    def _subscriber_layout(self, handle: MediaHandle, reply: Any) -> None:
        rows = getattr(reply.data, "streams", None)
        if rows is None:
            return
        if len(rows) > 256:
            raise OperationError("OUTCOME_UNKNOWN")
        streams = []
        mids: set[str] = set()
        for row in rows:
            feed, mid, feed_mid = janus_id(row.feed_id), row.mid, row.feed_mid
            if (
                not isinstance(mid, str)
                or not 1 <= len(mid) <= 128
                or mid in mids
                or row.type not in {"audio", "video"}
                or (feed_mid is not None and not isinstance(feed_mid, str))
            ):
                raise OperationError("OUTCOME_UNKNOWN")
            mids.add(mid)
            active = getattr(row, "active", False) is True
            if active and (feed_mid is None or (feed, feed_mid) not in handle.selected):
                raise OperationError("OUTCOME_UNKNOWN")
            streams.append(
                self._stream(
                    handle,
                    mid,
                    row.type,
                    feed,
                    feed_mid,
                    handle.selected.get((feed, feed_mid)) if feed_mid is not None else None,
                    active=active,
                )
            )
            streams[-1]["is_ready"] = row.ready is True
        handle.streams = streams

    async def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        handle = self.handles.get("subscriber")
        if (
            handle is None
            or handle.pending_offer is None
            or payload["negotiation_id"] != handle.negotiation
        ):
            raise OperationError("STALE_NEGOTIATION")
        subscriber_answer(payload["answer"]["sdp"], handle.pending_offer)
        reply = await self._call(handle, "start", answer=Jsep(**payload["answer"]))
        handle.pending_offer = None
        return self._ack(handle, "start", reply)

    async def forward_events(self) -> None:
        while True:
            handle, event, expected_hangup = await self.events.get()
            try:
                async with self.lock:
                    self.check()
                    if self.handles.get(handle.ref.role) is not handle:
                        continue
                    if (
                        getattr(event, "sender", None) != handle.ref.janus_handle_id
                        or getattr(event, "session_id", None) != handle.ref.janus_session_id
                    ):
                        continue
                    jsep = self._jsep(event)
                    if jsep is not None:
                        key = jsep_key(getattr(event, "transaction", None), jsep)
                        if key not in handle.seen_jsep:
                            if handle.ref.role == "subscriber" and jsep["type"] == "offer":
                                if handle.pending_offer is not None:
                                    raise OperationError("STALE_NEGOTIATION")
                                try:
                                    self._subscriber_layout(handle, parse_videoroom_response(event))
                                except Exception:
                                    raise OperationError("OUTCOME_UNKNOWN") from None
                                self._subscriber_offer(handle, jsep["sdp"])
                            elif (
                                handle.ref.role != "publisher"
                                or jsep["type"] != "answer"
                                or handle.negotiation is None
                            ):
                                raise OperationError("STALE_NEGOTIATION")
                            else:
                                self._publisher_answer(jsep["sdp"], handle.tracks)
                            handle.seen_jsep.append(key)
                            self.emit(
                                "media.jsep",
                                {
                                    "handle_type": handle.ref.role,
                                    "negotiation_id": handle.negotiation,
                                    "jsep": jsep,
                                    "transaction": key[0],
                                },
                            )
                    kind = getattr(event, "janus", "")
                    if kind == "webrtcup":
                        handle.webrtc_ready = True
                        for stream in handle.streams:
                            stream["is_ready"] = stream["is_active"]
                        self.hub.topology(self.meeting)
                    if kind == "event":
                        kind = "topology_changed"
                    if kind in {"webrtcup", "media", "hangup", "detached", "topology_changed"}:
                        self.emit(
                            "media.event",
                            {
                                "handle_type": handle.ref.role,
                                "kind": kind,
                                "janus_handle_id": str(handle.ref.janus_handle_id),
                            },
                        )
                    if kind == "detached" or (kind == "hangup" and not expected_hangup):
                        raise OwnershipError("Media transport was lost.")
            finally:
                self.events.task_done()

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.hub.peers.pop((self.meeting, self.participant), None)
        for handle in tuple(self.handles.values()):
            if handle.ref.token not in self.runtime.registry.bindings:
                continue  # A cancelled/failed SDK call already disposed this token.
            try:
                await self.runtime.registry.detach(handle.ref)
            except Exception:
                # Runtime teardown still owns uncertain bindings and retains its lease.
                self.runtime.registry.cleanup_failed = True
        self.handles.clear()
        self.hub.topology(self.meeting)
