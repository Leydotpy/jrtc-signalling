"""Worker-local connection handoff; Django remains the generation authority."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .auth import AuthError

if TYPE_CHECKING:
    from .signalling import SocketSession


@dataclass
class Fence:
    generation: int = 0
    sessions: set[SocketSession] = field(default_factory=set)


class ConnectionHandoffs:
    # Never evict a high-water mark and accidentally admit a delayed check.
    # Multiworker routing/ownership remains the separate SG-15 boundary.
    LIMIT = 10_000
    TIMEOUT = 6.0

    def __init__(self) -> None:
        self.fences: dict[tuple[str, str], Fence] = {}

    @staticmethod
    def key(session: SocketSession) -> tuple[str, str]:
        claims = session.authorization.claims
        return claims["meeting_id"], claims["participant_id"]

    def check(self, session: SocketSession) -> None:
        fence = self.fences[self.key(session)]
        if session.superseded or session.authorization.claims["generation"] != fence.generation:
            raise AuthError("STALE_GENERATION")

    async def claim(self, session: SocketSession) -> None:
        key = self.key(session)
        if key not in self.fences:
            if len(self.fences) >= self.LIMIT:
                raise AuthError("OVERLOADED")
            self.fences[key] = Fence()
        fence = self.fences[key]
        generation = session.authorization.claims["generation"]
        if generation <= fence.generation:
            raise AuthError("STALE_GENERATION")
        # Install before awaiting. Keep *all* unfinished predecessors: cancelling
        # an intermediate handoff must not let its successor skip old cleanup.
        fence.generation = generation
        previous = tuple(fence.sessions)
        fence.sessions.add(session)
        for old in previous:
            old.superseded = True
            old.authorization.invalidate(old.authorization.minimum_epoch)
            old.revocation.set()
        try:
            async with asyncio.timeout(self.TIMEOUT):
                await asyncio.gather(*(old.finished.wait() for old in previous))
                self.check(session)
                if previous:
                    session.authorization.deadline = 0
                    await session.authorization.check("session.ping")
                self.check(session)
        except TimeoutError:
            raise AuthError("UNAVAILABLE") from None
        if session.endpoint.moderation is not None:
            session.endpoint.moderation.check(session.authorization)

    def release(self, session: SocketSession) -> None:
        fence = self.fences.get(self.key(session))
        if fence is not None:
            fence.sessions.discard(session)
