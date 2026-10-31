"""Signed, short-lived ownership RPC and conservative local lease fencing."""

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx2 as httpx

from .bus import BusConfig, BusError, parse

LEASE_SECONDS = 30
OPERATION_SECONDS = 3


class OwnershipError(RuntimeError):
    """Safe error: no credential, message or endpoint detail."""


def validate_owner_request(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "meeting_id",
        "incarnation",
        "action",
        "generation",
        "issued_at",
    }:
        raise ValueError("Invalid ownership request.")
    for name in ("request_id", "meeting_id", "incarnation"):
        if not isinstance(value[name], str) or str(UUID(value[name])) != value[name]:
            raise ValueError("Invalid ownership identity.")
    if value["action"] not in {"claim", "renew", "release"}:
        raise ValueError("Invalid ownership action.")
    if type(value["generation"]) is not int or not 0 <= value["generation"] < 2**53:
        raise ValueError("Invalid ownership generation.")
    if type(value["issued_at"]) is not int or value["issued_at"] <= 0:
        raise ValueError("Invalid ownership time.")
    return value


@dataclass(frozen=True, slots=True)
class OwnerLease:
    meeting_id: str
    owner_id: str
    incarnation: str
    generation: int
    deadline: float

    def require(self, owner: str, generation: int, *, now: float) -> None:
        if (
            type(generation) is not int
            or owner != self.owner_id
            or generation != self.generation
            or now >= self.deadline
        ):
            raise OwnershipError("Ownership is unavailable or stale.")


class OwnershipClient:
    def __init__(self, config: BusConfig, url: str, incarnation: str) -> None:
        self.config, self.url, self.incarnation = config, url, incarnation
        self.client = httpx.AsyncClient(
            timeout=2, follow_redirects=False, limits=httpx.Limits(max_connections=4)
        )

    async def request(self, meeting_id: str, action: str, generation: int = 0) -> OwnerLease:
        started = asyncio.get_running_loop().time()
        body = {
            "request_id": str(uuid4()),
            "meeting_id": meeting_id,
            "incarnation": self.incarnation,
            "action": action,
            "generation": generation,
            "issued_at": int(time.time()),
        }
        packet = self.config.signer.sign(body, "owner-request")
        try:
            async with (
                asyncio.timeout(2),
                self.client.stream(
                    "POST",
                    self.url,
                    content=packet,
                    headers={
                        "Authorization": "Bearer " + (self.config.authority_token or ""),
                        "Content-Type": "application/json",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise OwnershipError("Ownership authority refused the request.")
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=1024):
                    raw.extend(chunk)
                    if len(raw) > 2048:
                        raise OwnershipError("Invalid ownership response.")
                result = parse(bytes(raw))
            if (
                set(result)
                != {
                    "request_id",
                    "meeting_id",
                    "owner_id",
                    "incarnation",
                    "generation",
                    "lease_seconds",
                }
                or any(result[k] != body[k] for k in ("request_id", "meeting_id", "incarnation"))
                or result["owner_id"] != self.config.signer.producer
                or type(result["generation"]) is not int
                or not 1 <= result["generation"] < 2**53
                or (generation and result["generation"] != generation)
                or type(result["lease_seconds"]) not in (int, float)
                or not math.isfinite(result["lease_seconds"])
                or not 0 <= result["lease_seconds"] <= LEASE_SECONDS
                or (action != "release" and result["lease_seconds"] < 5)
            ):
                raise OwnershipError("Invalid ownership response.")
            # Start before RPC and reserve a full second: a delayed response can
            # never extend the local lease beyond Django's database lease.
            return OwnerLease(
                meeting_id,
                result["owner_id"],
                self.incarnation,
                result["generation"],
                started + result["lease_seconds"] - 1,
            )
        except (BusError, ValueError, httpx.HTTPError, TimeoutError):
            raise OwnershipError("Ownership authority unavailable.") from None

    async def close(self) -> None:
        await self.client.aclose()
