"""Owned lifecycle and cached dependency readiness. Live adapters arrive in SG-06/07."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .config import Settings

logger = logging.getLogger("jrtc_signalling.lifecycle")


class ComponentStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class DependencyHealth:
    janus: ComponentStatus = ComponentStatus.NOT_CONFIGURED
    broka: ComponentStatus = ComponentStatus.NOT_CONFIGURED
    authority: ComponentStatus = ComponentStatus.NOT_CONFIGURED
    ownership: ComponentStatus = ComponentStatus.NOT_CONFIGURED

    def as_dict(self) -> dict[str, str]:
        return {
            "janus": self.janus.value,
            "broka": self.broka.value,
            "authority": self.authority.value,
            "ownership": self.ownership.value,
        }

    @property
    def ready(self) -> bool:
        return all(value == ComponentStatus.READY for value in self.as_dict().values())


class Runtime(Protocol):
    """Adapters must be cancellation-cooperative; health() reads cached state only."""

    async def start(self) -> None: ...
    async def drain(self) -> None: ...
    async def aclose(self) -> None: ...
    def health(self) -> DependencyHealth: ...


class ScaffoldRuntime:
    """No network connections, sessions, producers, or mock-success readiness."""

    async def start(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def health(self) -> DependencyHealth:
        return DependencyHealth()


class LifecycleError(RuntimeError):
    """Lifecycle errors deliberately omit underlying input or credential values."""


class Lifecycle:
    def __init__(self, runtime: Runtime, settings: Settings) -> None:
        self.runtime = runtime
        self.settings = settings
        self.phase = "starting"
        self.instance_id = str(uuid4())  # Diagnostic only, NEVER an owner fence.
        self._shutdown_task: asyncio.Task[None] | None = None

    def _log(self, event: str) -> None:
        logger.info(event, extra={"gateway_event": event})

    async def start(self) -> None:
        if self.phase != "starting":
            raise LifecycleError("Gateway lifecycle cannot be started twice.")
        self._log("gateway.starting")
        try:
            async with asyncio.timeout(self.settings.startup_timeout):
                await self.runtime.start()
        except asyncio.CancelledError:
            # Failure is retained in phase/logs; preserve caller cancellation.
            with suppress(LifecycleError):
                await self.stop()
            raise
        except Exception:
            await self.stop()
            self.phase = "failed"
            self._log("gateway.failed")
            raise LifecycleError("Gateway startup failed.") from None
        self.phase = "running"
        self._log("gateway.started")

    async def _shutdown(self) -> None:
        self.phase = "draining"
        self._log("gateway.draining")
        deadline = asyncio.get_running_loop().time() + self.settings.shutdown_timeout
        failed = False
        try:
            async with asyncio.timeout_at(deadline):
                await self.runtime.drain()
        except (Exception, asyncio.CancelledError):
            failed = True
        finally:
            # Always try closing resources, even when drain fails or exhausts budget.
            try:
                async with asyncio.timeout_at(deadline):
                    await self.runtime.aclose()
            except (Exception, asyncio.CancelledError):
                failed = True
        self.phase = "failed" if failed else "stopped"
        self._log("gateway.failed" if failed else "gateway.stopped")
        if failed:
            raise LifecycleError("Gateway shutdown did not complete cleanly.") from None

    async def stop(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown(), name="gateway-shutdown")
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._shutdown_task)
                break
            except asyncio.CancelledError:
                # Repeated cancellation must not abandon or cancel owned cleanup.
                cancelled = True
                if self._shutdown_task.cancelled():
                    self.phase = "failed"
                    break
            except LifecycleError:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
        if cancelled:
            raise asyncio.CancelledError

    def health(self) -> DependencyHealth:
        try:
            return self.runtime.health()
        except Exception:
            return DependencyHealth(*(ComponentStatus.UNAVAILABLE for _ in range(4)))
