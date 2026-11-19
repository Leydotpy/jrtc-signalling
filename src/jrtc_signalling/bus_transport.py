"""Broka's public transport SPI, with signed bytes and explicit durable ACKs.

No pickle/type-driven deserialization and no dependency on the legacy janus.events
consumer. Each owner has a stream; only one Django outcome consumer group exists.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import uuid4

from broka.engines.base import (
    EngineConsumer,
    EngineIncomingMessage,
    EnginePublishContext,
    EnginePublishResult,
    EngineSubscription,
)
from broka.engines.redis import RedisEngine

from .bus import MAX_PACKET, BusError

# Redis 7 MAXLEN can evict an unread or pending control decision. At capacity,
# reclaim only the delivered prefix before the oldest pending entry. Refuse
# writes when the sole expected group has no reclaimable space. Existing
# durable outboxes retry after backpressure. The check and append are atomic.
CAPPED_APPEND = """
local stream = KEYS[1]
local cap = tonumber(ARGV[1])
if redis.call('XLEN', stream) >= cap then
    local groups = redis.call('XINFO', 'GROUPS', stream)
    if #groups ~= 1 then return false end
    local group = {}
    for i = 1, #groups[1], 2 do group[groups[1][i]] = groups[1][i+1] end
    local pending = redis.call('XPENDING', stream, group.name)
    local floor = pending[2] or group['last-delivered-id']
    redis.call('XTRIM', stream, 'MINID', floor)
    if redis.call('XLEN', stream) >= cap then return false end
end
return redis.call('XADD', stream, '*', 'payload', ARGV[2], 'headers', ARGV[3], 'message_id', ARGV[4])
"""


class ControlEngine(RedisEngine):
    """Broka 0.0.2's Redis Streams envelope with lossless admission at the cap."""

    capacity = 4096

    async def publish(
        self, destination: str, payload: bytes, context: EnginePublishContext
    ) -> EnginePublishResult:
        raw = await self._client.eval(
            CAPPED_APPEND,
            1,
            destination,
            self.capacity,
            payload,
            json.dumps(dict(context.headers), separators=(",", ":")),
            context.message_id,
        )
        return EnginePublishResult(accepted=bool(raw), transport_id=raw.decode() if raw else None)

    async def group_depth(self, route: str) -> tuple[int | None, int | None]:
        if self._client is not None:
            for row in await self._client.xinfo_groups(route):
                if row["name"].decode() == self.config["group"]:
                    return row.get("lag"), row["pending"]
        return None, None


class Streams:
    def __init__(self, url: str, group: str, *, claim_idle_ms: int = 10000) -> None:
        self.consumers: list[EngineConsumer] = []
        self.engine = ControlEngine(
            {
                "url": url,
                "mode": "streams",
                "group": group,
                "consumer_name": str(uuid4()),
                "claim_idle_ms": claim_idle_ms,
                "claim_interval": max(0.05, claim_idle_ms / 2000),
            }
        )

    async def start(self) -> None:
        async with asyncio.timeout(3):
            await self.engine.connect()

    async def close(self) -> None:
        async with asyncio.timeout(3):
            await self.engine.disconnect()
        self.consumers.clear()

    async def healthy(self) -> bool:
        # Broka 0.0.2 reports stream-loop failures but misses a failure while
        # creating the group. Inspect its pinned consumer task so that ACL/startup
        # errors cannot leave the service falsely healthy and silently idle.
        for consumer in self.consumers:
            task = getattr(consumer, "_task", None)
            if not isinstance(task, asyncio.Task) or task.done():
                if isinstance(task, asyncio.Task) and not task.cancelled():
                    task.exception()
                return False
        async with asyncio.timeout(2):
            return (await self.engine.healthcheck()).healthy

    async def publish(self, route: str, packet: bytes, message_id: str) -> None:
        if len(packet) > MAX_PACKET:
            raise BusError("Control message exceeds limit.")
        async with asyncio.timeout(2):
            result = await self.engine.publish(
                route, packet, EnginePublishContext(message_id=message_id)
            )
            if not result.accepted:
                raise BusError("Control publish unavailable.")

    async def subscribe(
        self, route: str, handle: Callable[[EngineIncomingMessage], Awaitable[None]]
    ) -> EngineConsumer:
        # RedisEngine awaits this callback directly. There is no unbounded task
        # spawning; a slow consumer leaves the remaining messages in Redis.
        consumer = await self.engine.create_consumer(
            EngineSubscription(
                id=route, pattern=route, destination=route, concurrency=1, capacity=8
            ),
            handle,
        )
        self.consumers.append(consumer)
        return consumer
