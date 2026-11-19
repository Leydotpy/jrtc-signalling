"""Fixed-memory observations shared by the gateway and Django's SSE producer."""

import asyncio
import hashlib
import hmac
import math
import re
from bisect import bisect_left
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any

from .config import ConfigurationError

BOUNDS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
AUTH_CODES = (
    "unauthenticated",
    "forbidden",
    "expired_ticket",
    "stale_authorization",
    "out_of_order",
    "unavailable",
    "overloaded",
    "rate_limited",
    "other",
)


def read_key(path: str) -> bytes:
    if not path:
        return b""
    try:
        with Path(path).open("rb") as source:
            key = source.read(258).removesuffix(b"\n")
        if re.fullmatch(rb"[A-Za-z0-9._~-]{32,256}", key):
            return key
    except OSError:
        pass
    raise ConfigurationError("Invalid observability key file.")


def authorized(key: bytes, secure: bool, header: str) -> bool:
    return bool(
        key
        and secure
        and header.isascii()
        and len(header) <= 263
        and hmac.compare_digest(b"Bearer " + key, header.encode("ascii"))
    )


def trace_ref(key: bytes, field: str, identity: str) -> str:
    return hmac.new(
        key, f"jrtc-signalling/v1/{field}/{identity}".encode(), hashlib.sha256
    ).hexdigest()[:32]


class Histogram:
    """Store disjoint counts. Build cumulative Prometheus buckets only at scrape time."""

    def __init__(self) -> None:
        self._buckets = [0] * (len(BOUNDS) + 1)
        self._sum = 0.0
        self._lock = Lock()

    def observe(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self._lock:
            self._buckets[bisect_left(BOUNDS, seconds)] += 1
            self._sum += seconds

    def render(self, name: str) -> str:
        with self._lock:
            counts, total = self._buckets.copy(), self._sum
        lines = [f"# TYPE {name} histogram"]
        cumulative = 0
        for bound, count in zip((*BOUNDS, "+Inf"), counts, strict=True):
            cumulative += count
            lines.append(f'{name}_bucket{{le="{bound}"}} {cumulative}')
        lines.extend((f"{name}_sum {total}", f"{name}_count {cumulative}"))
        return "\n".join(lines)


class GatewayTelemetry:
    def __init__(self, trace_key: bytes = b"") -> None:
        self.trace_key = trace_key
        self.handshakes: Counter[str] = Counter()
        self.resumes: Counter[str] = Counter()
        self.auth_errors: Counter[str] = Counter()
        self.ice = Histogram()
        self.queue = Histogram()
        self.loop = Histogram()
        self.relayed = 0

    def handshake(self, *, success: bool, resume: bool = False) -> None:
        outcome = "success" if success else "failure"
        self.handshakes[outcome] += 1
        if resume:
            self.resumes[outcome] += 1

    def auth_error(self, code: str) -> None:
        reason = code.lower()
        self.auth_errors[reason if reason in AUTH_CODES else "other"] += 1

    async def sample_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            target = loop.time() + 0.25
            await asyncio.sleep(0.25)
            self.loop.observe(max(0, loop.time() - target))

    def render(self, endpoint: Any, runtime: Any) -> str:
        lines = [
            self.ice.render("jrtc_ice_ack_seconds"),
            self.queue.render("jrtc_ice_queue_seconds"),
            self.loop.render("jrtc_event_loop_lag_seconds"),
        ]
        for name, counts, label, values in (
            ("handshakes", self.handshakes, "outcome", ("success", "failure")),
            ("resumes", self.resumes, "outcome", ("success", "failure")),
            ("authorization_errors", self.auth_errors, "reason", AUTH_CODES),
        ):
            lines.append(f"# TYPE jrtc_{name}_total counter")
            lines.extend(f'jrtc_{name}_total{{{label}="{v}"}} {counts[v]}' for v in values)
        lines.append(f"jrtc_relayed_connections_total {self.relayed}")
        if endpoint is not None:
            lines.extend(
                (
                    f"jrtc_websocket_connections {len(endpoint.connections)}",
                    f"jrtc_dispatch_active {endpoint.metrics.active}",
                    f"jrtc_dispatch_overloads_total {endpoint.metrics.overloads}",
                    f"jrtc_ice_dispatch_errors_total {endpoint.metrics.operations['media.trickle'].errors}",
                )
            )
            if endpoint.moderation is not None:
                moderation = endpoint.moderation
                lines.append(f"jrtc_permission_rooms_pending {len(moderation.pending_rooms)}")
                for outcome in (
                    "barriers",
                    "applied",
                    "rate_limited",
                    "rooms_reconciled",
                    "cleanup_retry",
                ):
                    lines.append(
                        f'jrtc_permission_reconciliation_total{{outcome="{outcome}"}} {moderation.counters[outcome]}'
                    )
        if hasattr(runtime, "registry") and hasattr(runtime, "manager"):
            sessions = tuple(s for s in runtime.manager.sessions if s.ready)
            lines.extend(
                (
                    f"jrtc_janus_sessions {len(sessions)}",
                    f"jrtc_janus_handles {sum(len(s.plugins) for s in sessions)}",
                    f"jrtc_janus_uncertain_handles {len(runtime.registry.uncertain)}",
                    f"jrtc_owned_meetings {len(runtime.leases)}",
                )
            )
        control = getattr(runtime, "control", runtime)
        if hasattr(control, "consumer"):
            consumer = control.consumer
            lines.extend(
                (
                    consumer.delivery_age.render("jrtc_command_delivery_age_seconds"),
                    f"jrtc_command_dlq_total {consumer.counters['quarantined'] + consumer.counters['conflict']}",
                    f"jrtc_command_delivery_failures_total {consumer.counters['pending_failure']}",
                    f"jrtc_command_reader_up {int(control.status.value == 'ready')}",
                )
            )
            for name, value in zip(("lag", "pending"), control.group_depth, strict=True):
                lines.append(f"jrtc_command_group_{name} {value if value is not None else 'NaN'}")
        return "\n".join(lines) + "\n"
