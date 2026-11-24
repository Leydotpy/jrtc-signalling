"""Current-owner moderation barriers; an applied result requires physical cleanup."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, Any

from .auth import AuthError, AuthorizationSession
from .ownership import OwnershipError

if TYPE_CHECKING:
    from .websocket_auth import AuthenticatedEndpoint


class ModerationEnforcer:
    def __init__(self, endpoint: AuthenticatedEndpoint) -> None:
        from .websocket_auth import RateLimit

        self.endpoint = endpoint
        self.rate = RateLimit(burst=20, per_second=10)
        # Bounds match the durable inbox. Exhaustion closes that meeting, rather
        # than evicting a fence that an in-flight authority response could bypass.
        self.epochs: dict[tuple[str, str], tuple[int, bool]] = {}
        self.terminated: set[str] = set()
        self.pending_rooms: dict[str, int] = {}
        self.counters: Counter[str] = Counter()

    def check(self, authorization: AuthorizationSession) -> None:
        claims = authorization.claims
        meeting, participant = claims["meeting_id"], claims["participant_id"]
        epoch, inclusive = self.epochs.get((meeting, participant), (-1, False))
        if (
            meeting in self.terminated
            or claims["authorization_epoch"] < epoch
            or (inclusive and claims["authorization_epoch"] == epoch)
        ):
            authorization.invalidate(max(epoch, claims["authorization_epoch"]))
            raise AuthError("STALE_AUTHORIZATION")

    async def apply(self, command: dict[str, Any]) -> tuple[str, str]:
        hub = self.endpoint.media
        if hub is None:
            return "failed", "unavailable"
        runtime = hub.runtime
        meeting, generation = command["meeting_id"], command["expected_owner_generation"]
        runtime.require(meeting, generation)
        kind, participant, epoch = (
            command[k] for k in ("type", "participant_id", "authorization_epoch")
        )
        exhausted = False
        if kind == "meeting.terminate":
            self.terminated.add(meeting)
            self.pending_rooms[meeting] = generation
        elif kind in {
            "participant.disconnect",
            "participant.deny",
            "participant.permissions.update",
            "participant.admit",
        }:
            key = meeting, participant
            previous, inclusive = self.epochs.get(key, (-1, False))
            if (
                (
                    meeting in self.terminated
                    and kind in {"participant.admit", "participant.permissions.update"}
                )
                or epoch < previous
                or (
                    epoch == previous
                    and inclusive
                    and kind in {"participant.admit", "participant.permissions.update"}
                )
                or any(
                    s.authorization.claims["meeting_id"] == meeting
                    and s.authorization.claims["participant_id"] == participant
                    and s.authorization.claims["authorization_epoch"] > epoch
                    for s in self.endpoint.sessions
                )
            ):
                return "failed", "revoked"
            if key not in self.epochs and len(self.epochs) >= 10000:
                self.terminated.add(meeting)
                exhausted = True
            else:
                self.epochs[key] = (
                    epoch,
                    kind in {"participant.disconnect", "participant.deny"}
                    or (epoch == previous and inclusive),
                )
        else:
            return "failed", "unavailable"

        # Install the barrier before any await, including pending hello/refresh
        # RPCs. Waking the session cancels queued and in-flight SDK work; its
        # existing shielded cleanup joins those operations before detaching.
        closing = []
        for session in tuple(self.endpoint.sessions):
            if session.authorization.claims["meeting_id"] != meeting:
                continue
            try:
                self.check(session.authorization)
            except AuthError:
                session.revocation.set()
                closing.append(session)
        self.counters["barriers"] += 1
        if not self.rate.take():
            # Rejection never delays the deny fence or socket cancellation.
            # Cleanup remains owned by the session/reconciliation worker.
            self.counters["rate_limited"] += 1
            return "failed", "unavailable"
        if closing:
            await asyncio.gather(*(s.finished.wait() for s in closing))
        runtime.require(meeting, generation)
        await runtime.registry.reconcile(
            meeting,
            generation,
            participant=participant,
            preserve={
                h.ref.token
                for peer in hub.peers.values()
                if not peer.closed
                for h in peer.handles.values()
            },
        )
        if kind == "meeting.terminate":
            await hub.terminate(meeting, generation)
            self.pending_rooms.pop(meeting, None)
        runtime.require(meeting, generation)
        if meeting in runtime.registry.unresolved_meetings:
            raise OwnershipError("Unidentified handles require owner reconciliation.")
        if exhausted:
            return "failed", "unavailable"
        self.counters["applied"] += 1
        return "applied", "normal"

    async def reconcile(self) -> None:
        hub = self.endpoint.media
        assert hub is not None
        for meeting, generation in tuple(self.pending_rooms.items()):
            if any(s.authorization.claims["meeting_id"] == meeting for s in self.endpoint.sessions):
                continue
            try:
                async with asyncio.timeout(3):
                    hub.runtime.require(meeting, generation)
                    await hub.terminate(meeting, generation)
                self.pending_rooms.pop(meeting, None)
                self.counters["rooms_reconciled"] += 1
            except Exception:
                self.counters["cleanup_retry"] += 1
