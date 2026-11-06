"""Bounded ICE validation and SDP credential binding, without an SDK dependency."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .contracts import OperationError
from .sdp import sections

MAX_GENERATION_CANDIDATES = 256
MAX_GENERATION_BYTES = 256 * 1024


def candidate_fragment(candidate: dict[str, Any]) -> str | None:
    """Validate candidate syntax without resolving addresses or logging input."""
    value = candidate["candidate"]
    if not re.fullmatch(r"[\x21-\x7e]+(?: [\x21-\x7e]+)*", value):
        raise OperationError()
    parts = value.split(" ")
    if (
        len(parts) < 8
        or (len(parts) - 8) % 2
        or not re.fullmatch(r"candidate:[A-Za-z0-9+/]{1,32}", parts[0])
        or parts[1] not in {"1", "2"}
        or parts[2].lower() not in {"udp", "tcp"}
        or not re.fullmatch(r"[0-9]{1,10}", parts[3])
        or not 0 < int(parts[3]) < 2**32
        or len(parts[4]) > 253
        or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", parts[4])
        or not re.fullmatch(r"[0-9]{1,5}", parts[5])
        or not 0 < int(parts[5]) <= 65535
        or parts[6] != "typ"
        or parts[7] not in {"host", "srflx", "prflx", "relay"}
    ):
        raise OperationError()
    index = candidate.get("sdpMLineIndex")
    if index is not None and type(index) is not int:
        raise OperationError()
    fragments = [parts[i + 1] for i in range(8, len(parts), 2) if parts[i] == "ufrag"]
    fragment = candidate.get("usernameFragment")
    if len(fragments) > 1 or (fragments and fragment is not None and fragments[0] != fragment):
        raise OperationError()
    return fragments[0] if fragments else fragment


@dataclass(frozen=True, repr=False)
class IceSection:
    mid: str
    active: bool
    ufrag: str | None
    password: str | None
    completed: bool


@dataclass(frozen=True, repr=False)
class IceDescription:
    media: tuple[IceSection, ...]

    @property
    def key(self) -> str:
        credentials = sorted(self.credentials, key=lambda c: (c[0] or "", c[1] or ""))
        return hashlib.sha256(json.dumps(credentials).encode()).hexdigest()

    @property
    def credentials(self) -> frozenset[tuple[str | None, str | None]]:
        return frozenset((s.ufrag, s.password) for s in self.media if s.active)

    @property
    def completed(self) -> bool:
        return all(s.completed for s in self.media if s.active)

    def require_restart(self, previous: IceDescription) -> None:
        # Both parts must change for a new generation; a new wire UUID alone
        # cannot turn delayed candidates into candidates for a restarted agent.
        old_fragments = {c[0] for c in previous.credentials}
        old_passwords = {c[1] for c in previous.credentials}
        if not self.credentials or any(
            u in old_fragments or p in old_passwords for u, p in self.credentials
        ):
            raise OperationError("STALE_NEGOTIATION")

    def bind(self, candidate: dict[str, Any]) -> dict[str, Any]:
        fragment = candidate_fragment(candidate)
        mid, index = candidate.get("sdpMid"), candidate.get("sdpMLineIndex")
        if index is None:
            index = next((i for i, s in enumerate(self.media) if s.mid == mid), -1)
        if not 0 <= index < len(self.media):
            raise OperationError("STALE_NEGOTIATION")
        section = self.media[index]
        if (
            not section.active
            or (mid is not None and mid != section.mid)
            or (fragment is not None and fragment != section.ufrag)
        ):
            raise OperationError("STALE_NEGOTIATION")
        # JRTC intentionally has no usernameFragment field. It has been checked
        # against browser SDP before being removed at this SDK boundary.
        return {k: v for k, v in candidate.items() if k != "usernameFragment"}


def description(sdp: str) -> IceDescription:
    media = sections(sdp)
    session: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for line in sdp.splitlines():
        if line.startswith("m="):
            rows.append({})
        elif line.startswith(("a=ice-ufrag:", "a=ice-pwd:")) or line == "a=end-of-candidates":
            key, _, value = line.partition(":")
            target = rows[-1] if rows else session
            if key in target:
                raise OperationError()
            target[key] = value
    result = []
    for section, row in zip(media, rows, strict=True):
        attributes = session | row
        fragment, password = attributes.get("a=ice-ufrag"), attributes.get("a=ice-pwd")
        if section.active and (
            fragment is None
            or password is None
            or not re.fullmatch(r"[A-Za-z0-9+/]{4,256}", fragment)
            or not re.fullmatch(r"[A-Za-z0-9+/]{22,256}", password)
        ):
            raise OperationError()
        result.append(
            IceSection(
                section.mid, section.active, fragment, password, "a=end-of-candidates" in attributes
            )
        )
    return IceDescription(tuple(result))


@dataclass(repr=False)
class IceState:
    description: IceDescription
    completed: bool = False
    count: int = 0
    bytes: int = 0
    retired: frozenset[str] = frozenset()

    @classmethod
    def negotiate(cls, current: IceDescription, previous: IceState | None) -> IceState:
        if previous is not None and current.credentials == previous.description.credentials:
            return cls(
                current,
                previous.completed or current.completed,
                previous.count,
                previous.bytes,
                previous.retired,
            )
        retired = previous.retired | {previous.description.key} if previous else frozenset()
        if current.key in retired:
            raise OperationError("STALE_NEGOTIATION")
        if len(retired) >= 64:
            raise OperationError("OVERLOADED")
        return cls(current, current.completed, retired=retired)

    def prepare(self, candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        if self.completed and candidates:
            raise OperationError("STALE_NEGOTIATION")
        size = len(json.dumps(candidates, separators=(",", ":"), ensure_ascii=False).encode())
        if (
            self.count + len(candidates) > MAX_GENERATION_CANDIDATES
            or self.bytes + size > MAX_GENERATION_BYTES
        ):
            raise OperationError("OVERLOADED")
        return [self.description.bind(c) for c in candidates], size
