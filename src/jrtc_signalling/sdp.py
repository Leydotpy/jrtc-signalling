"""Bounded media-policy inspection; Janus remains the SDP protocol implementation."""

import re
from dataclasses import dataclass
from typing import Any

from .auth import AuthError
from .contracts import OperationError

DIRECTIONS = {"sendrecv", "sendonly", "recvonly", "inactive"}
SOURCES = {
    "microphone": ("audio", "audio"),
    "camera": ("video", "video"),
    "screen_share": ("video", "screen"),
    "screen_share_audio": ("audio", "screen"),
}


@dataclass(frozen=True)
class Section:
    mid: str
    kind: str
    active: bool
    direction: str


def sections(sdp: str, *, limit: int = 256) -> list[Section]:
    """Read all m-lines/MIDs/directions, rejecting ambiguous or unsupported media.

    This is a policy gate, not a replacement offer/answer parser. Never include
    input SDP in errors. Capture-device provenance is not attested by SDP.
    """
    if not sdp.startswith("v=0") or len(sdp.encode()) > 65536:
        raise OperationError()
    normalized = sdp.replace("\r\n", "\n")
    if any(ord(c) < 32 and c != "\n" for c in normalized):
        raise OperationError()
    lines = normalized.splitlines()
    if (
        not lines
        or lines[0] != "v=0"
        or any(not re.fullmatch(r"[a-z]=[^\x00-\x1f\x7f]*", line) for line in lines)
    ):
        raise OperationError()
    session_direction = "sendrecv"
    session_direction_seen = False
    media: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("m="):
            parts = line[2:].split()
            if (
                len(parts) < 4
                or parts[0] not in {"audio", "video"}
                or not re.fullmatch(r"[0-9]{1,5}", parts[1])
                or not 0 <= int(parts[1]) <= 65535
                or parts[2] not in {"UDP/TLS/RTP/SAVPF", "RTP/SAVPF"}
                or len(media) >= limit
            ):
                raise OperationError()
            media.append(
                {
                    "kind": parts[0],
                    "active": int(parts[1]) != 0,
                    "mid": "",
                    "direction": session_direction,
                    "explicit": False,
                }
            )
        elif line.startswith("a=mid:"):
            mid = line[6:]
            if not media or media[-1]["mid"] or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", mid):
                raise OperationError()
            media[-1]["mid"] = mid
        elif line[2:] in DIRECTIONS and line.startswith("a="):
            if media:
                if media[-1]["explicit"]:
                    raise OperationError()
                media[-1].update(direction=line[2:], explicit=True)
            else:
                if session_direction_seen:
                    raise OperationError()
                session_direction, session_direction_seen = line[2:], True
    mids = [m["mid"] for m in media]
    if not media or "" in mids or len(set(mids)) != len(mids):
        raise OperationError()
    return [Section(m["mid"], m["kind"], m["active"], m["direction"]) for m in media]


def publisher_tracks(
    sdp: str, tracks: list[dict[str, Any]], permissions: dict[str, bool]
) -> dict[str, dict[str, str]]:
    active = {s.mid: s for s in sections(sdp, limit=16) if s.active and s.direction != "inactive"}
    if any(s.direction not in {"sendonly", "sendrecv"} for s in active.values()):
        raise AuthError("FORBIDDEN")
    mapped: dict[str, dict[str, str]] = {}
    for track in tracks:
        kind, permission = SOURCES[track["source"]]
        if track["kind"] != kind or not permissions[permission]:
            raise AuthError("FORBIDDEN")
        mid = track.get("mid")
        if not mid:
            candidates = [m for m, s in active.items() if s.kind == kind and m not in mapped]
            if len(candidates) != 1:
                raise OperationError()
            mid = candidates[0]
        if mid not in active or mid in mapped or active[mid].kind != kind:
            raise AuthError("FORBIDDEN")
        mapped[mid] = {"kind": kind, "source": track["source"]}
    if set(mapped) != set(active):
        raise AuthError("FORBIDDEN")
    return mapped


def subscriber_answer(sdp: str, offer: list[Section]) -> None:
    answer = sections(sdp)
    if [(s.mid, s.kind) for s in answer] != [(s.mid, s.kind) for s in offer]:
        raise OperationError("STALE_NEGOTIATION")
    if any(s.active and s.direction not in {"recvonly", "inactive"} for s in answer):
        raise AuthError("FORBIDDEN")
    if any(a.active and not o.active for a, o in zip(answer, offer, strict=True)):
        raise OperationError()
