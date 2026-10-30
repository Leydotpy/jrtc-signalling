"""Bounded durable command processing. Media ownership/enforcement is SG-07/13."""

import asyncio
import json
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol

import httpx2 as httpx
from broka.engines.base import EngineIncomingMessage

from .bus import EVENTS, BusConfig, BusError, canonical, command_route, digest, parse
from .bus_transport import Streams
from .command_store import CommandStore
from .runtime import ComponentStatus, DependencyHealth


class CommandAuthority(Protocol):
    async def check(self, command: dict[str, Any]) -> bool: ...


class Enforcer(Protocol):
    """Must fence current local owner/generation/epoch immediately at mutation.

    Execution is never repeated after a reserved command has an unknown result.
    An applied return must mean actual enforcement, not queue admission.
    """

    async def apply(self, command: dict[str, Any]) -> tuple[str, str]: ...


class NoOwnerEnforcer:
    async def apply(self, command: dict[str, Any]) -> tuple[str, str]:
        return "failed", "owner_lost"


class HTTPCommandAuthority:
    def __init__(self, config: BusConfig) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=2, follow_redirects=False, limits=httpx.Limits(max_connections=4)
        )

    async def check(self, command: dict[str, Any]) -> bool:
        if self.config.authority_url is None or self.config.authority_token is None:
            return False
        async with (
            asyncio.timeout(2),
            self.client.stream(
                "POST",
                self.config.authority_url,
                content=canonical({"command": command}),
                headers={
                    "Authorization": "Bearer " + self.config.authority_token,
                    "Content-Type": "application/json",
                },
            ) as response,
        ):
            if response.status_code != 200:
                return False
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=1024):
                body.extend(chunk)
                if len(body) > 2048:
                    return False
            result = parse(bytes(body))
            return set(result) == {"authorized"} and result["authorized"] is True

    async def close(self) -> None:
        await self.client.aclose()


class CommandConsumer:
    def __init__(
        self,
        config: BusConfig,
        store: CommandStore,
        authority: CommandAuthority,
        enforcer: Enforcer | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config, self.store, self.authority = config, store, authority
        self.enforcer = enforcer or NoOwnerEnforcer()
        self.clock = clock
        self.counters: Counter[str] = Counter()

    async def handle(self, delivery: EngineIncomingMessage) -> None:
        try:
            try:
                producer, command = self.config.verifier.verify(delivery.payload, "command")
                if (
                    producer != "django-control"
                    or command["target_owner"] != self.config.signer.producer
                ):
                    raise BusError("Wrong command destination.")
            except BusError:
                await asyncio.to_thread(
                    self.store.quarantine, digest(delivery.payload), "untrusted", self.clock()
                )
                await delivery.acknowledgement.ack()
                self.counters["quarantined"] += 1
                return
            disposition = await asyncio.to_thread(self.store.begin, command, self.clock())
            if disposition == "busy":
                return  # Stay in Redis's pending list; no concurrent execution.
            if disposition == "conflict":
                await asyncio.to_thread(
                    self.store.quarantine, digest(delivery.payload), "conflict", self.clock()
                )
            if disposition == "new":
                status, reason = "failed", "unavailable"
                try:
                    async with asyncio.timeout(3):
                        now = self.clock()
                        issued = datetime.fromisoformat(
                            command["issued_at"].replace("Z", "+00:00")
                        ).timestamp()
                        expiry = datetime.fromisoformat(
                            command["expires_at"].replace("Z", "+00:00")
                        ).timestamp()
                        if not issued - 5 <= now < expiry:
                            reason = "expired"
                        elif not await self.authority.check(command):
                            reason = "revoked"
                        else:
                            status, reason = await self.enforcer.apply(command)
                            if status not in {"applied", "failed", "timed_out"}:
                                raise BusError("Invalid enforcement result.")
                except TimeoutError:
                    status, reason = "timed_out", "timeout"
                except Exception:
                    status, reason = "failed", "unavailable"
                await asyncio.to_thread(self.store.finish, command, status, reason, self.clock())
            await delivery.acknowledgement.ack()
            self.counters[disposition] += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Storage/ACK errors stay pending for XAUTOCLAIM. Never let a callback
            # exception kill Broka's stream reader or log an input/credential.
            self.counters["pending_failure"] += 1


class CommandRuntime:
    def __init__(self, config: BusConfig) -> None:
        if config.inbox_path is None:
            raise BusError("Persistent command inbox required.")
        self.config = config
        self.store = CommandStore(config.inbox_path)
        self.authority = HTTPCommandAuthority(config)
        self.consumer = CommandConsumer(config, self.store, self.authority)
        self.transport = Streams(config.redis_url, "gateway-control-v1")
        self.status = ComponentStatus.UNAVAILABLE
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self.store.initialize)
        self.task = asyncio.create_task(self.run(), name="gateway-control-plane")

    async def relay(self) -> None:
        for row in await asyncio.to_thread(self.store.pending, time.time()):
            try:
                packet = self.config.signer.sign(json.loads(row["body"]), "command-outcome")
                await self.transport.publish(EVENTS, packet, row["id"])
                await asyncio.to_thread(self.store.published, row["id"])
            except Exception:
                await asyncio.to_thread(self.store.failed_publish, row["id"], time.time())
                self.status = ComponentStatus.DEGRADED
                break

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                await self.transport.start()
                await self.transport.subscribe(
                    command_route(self.config.signer.producer), self.consumer.handle
                )
                while not self.stop.is_set():
                    await asyncio.to_thread(self.store.maintain, time.time())
                    await self.relay()
                    if not await self.transport.healthy():
                        raise BusError("Command transport unavailable.")
                    self.status = ComponentStatus.READY
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), 0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = ComponentStatus.DEGRADED
            finally:
                try:
                    await self.transport.close()
                except Exception:
                    self.status = ComponentStatus.DEGRADED
            if not self.stop.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), 1)

    async def drain(self) -> None:
        self.stop.set()

    async def aclose(self) -> None:
        self.stop.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.authority.close()

    def health(self) -> DependencyHealth:
        return DependencyHealth(broka=self.status)
