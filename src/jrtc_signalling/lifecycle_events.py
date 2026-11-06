"""Durable, bounded observation snapshots. Never called by a Janus reader.

One row reserves both the live and terminal state of each connection. Under
backpressure intermediate snapshots coalesce, but the latest complete state is
never evicted. This is state reconciliation, not an audit log of every edge.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import sqlite3
from collections import Counter
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from .bus import LIFECYCLE, BusConfig, canonical
from .bus_transport import Streams
from .contracts import validate_message
from .ownership import OwnershipError


def snapshot(connection: str, **publishing: bool) -> dict[str, Any]:
    return {"connection": connection, "audio": False, "video": False, "screen": False, **publishing}


def stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def envelope(
    previous: dict[str, Any], kind: str, state: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    value = {
        **previous,
        "event_id": str(uuid4()),
        "type": kind,
        "sequence": previous["sequence"] + 1,
        "occurred_at": stamp(),
        "snapshot": state,
        "data": data,
    }
    validate_message(value, schema="lifecycle-event")
    return value


class ObservationStore:
    """SQLite FULL commits; fixed row/payload budget; only delivered tombstones expire."""

    def __init__(self, path: str, *, capacity: int = 1024) -> None:
        self.path, self.capacity = path, capacity
        self.lock_file: BinaryIO | None = None

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=0.5)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA max_page_count=8192")  # 32 MiB at the default 4 KiB page size.
        return db

    def start(self, owner: str) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.path + ".lock", "ab")  # noqa: SIM115
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with closing(self.connect()) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS observations (stream TEXT PRIMARY KEY, body TEXT NOT NULL, pending INTEGER NOT NULL, terminal INTEGER NOT NULL, updated REAL NOT NULL)"
                )
                # A restarted process cannot resurrect live transports. Persist
                # their terminal replacement before replaying anything to Broka.
                for stream, raw, terminal in db.execute(
                    "SELECT stream,body,terminal FROM observations"
                ).fetchall():
                    body = json.loads(raw)
                    if body["gateway_owner_id"] != owner:
                        raise OwnershipError("Observation spool belongs to another owner.")
                    if not terminal:
                        body = envelope(
                            body,
                            "gateway.recovery_failed",
                            snapshot("OFFLINE"),
                            {"reason_code": "owner_lost"},
                        )
                        self._write(db, stream, body)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    @staticmethod
    def _write(db: sqlite3.Connection, stream: str, body: dict[str, Any]) -> None:
        raw = canonical(body)
        if len(raw) > 4096:
            raise OwnershipError("Observation exceeds spool limit.")
        db.execute(
            "INSERT OR REPLACE INTO observations VALUES (?,?,1,?,?)",
            (
                stream,
                raw.decode(),
                int(body["snapshot"]["connection"] == "OFFLINE"),
                datetime.now(UTC).timestamp(),
            ),
        )

    def write(self, stream: str, body: dict[str, Any], *, opening: bool = False) -> None:
        with closing(self.connect()) as db, db:
            if opening:
                db.execute(
                    "DELETE FROM observations WHERE terminal=1 AND pending=0 AND updated < ?",
                    (datetime.now(UTC).timestamp() - 86400,),
                )
                if (
                    db.execute("SELECT 1 FROM observations WHERE stream=?", (stream,)).fetchone()
                    or db.execute("SELECT count(*) FROM observations").fetchone()[0]
                    >= self.capacity
                ):
                    raise OwnershipError("Observation capacity or generation unavailable.")
            self._write(db, stream, body)

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        with closing(self.connect()) as db:
            return [
                (stream, json.loads(raw))
                for stream, raw in db.execute(
                    "SELECT stream,body FROM observations WHERE pending=1 ORDER BY updated LIMIT 20"
                )
            ]

    def delivered(self, stream: str, body: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            # A late ACK must never erase a newer pending terminal snapshot.
            db.execute(
                "UPDATE observations SET pending=0 WHERE stream=? AND body=?",
                (stream, canonical(body).decode()),
            )

    def replay(self) -> None:
        with closing(self.connect()) as db, db:
            db.execute(
                "DELETE FROM observations WHERE terminal=1 AND pending=0 AND updated < ?",
                (datetime.now(UTC).timestamp() - 86400,),
            )
            db.execute("UPDATE observations SET pending=1")


class ObservationConnection:
    def __init__(self, publisher: LifecyclePublisher, stream: str, body: dict[str, Any]) -> None:
        self.publisher, self.stream, self.body = publisher, stream, body
        self.lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return bool(self.body["snapshot"]["connection"] == "OFFLINE")

    async def record(
        self, kind: str, state: dict[str, Any], data: dict[str, Any], *, epoch: int | None = None
    ) -> None:
        async with self.lock:
            if self.closed:
                return
            body = envelope(
                {
                    **self.body,
                    "authorization_epoch": epoch
                    if epoch is not None
                    else self.body["authorization_epoch"],
                },
                kind,
                state,
                data,
            )
            try:
                await self.publisher.disk(self.publisher.store.write, self.stream, body)
            except asyncio.CancelledError:
                # disk joins the commit before propagating cancellation. Keep
                # its sequence so cleanup cannot reuse it for a different ID.
                self.committed(body)
                raise
            self.committed(body)

    def committed(self, body: dict[str, Any]) -> None:
        self.body = body
        self.publisher.wake.set()
        if self.closed:
            self.publisher.connections.pop(self.stream, None)

    async def close(self, *, lost: bool = False) -> None:
        if lost:
            await self.record(
                "gateway.session_lost", snapshot("RECOVERING"), {"reason_code": "transport_lost"}
            )
        await self.record(
            "gateway.recovery_failed" if lost else "participant.signaling_disconnected",
            snapshot("OFFLINE"),
            {"reason_code": "transport_lost" if lost else "normal"},
        )


class LifecyclePublisher:
    def __init__(self, config: BusConfig, *, capacity: int = 1024) -> None:
        if not config.inbox_path:
            raise OwnershipError("Lifecycle events require a durable spool.")
        self.config = config
        self.store = ObservationStore(config.inbox_path + ".observations", capacity=capacity)
        self.transport = Streams(config.redis_url, "gateway-lifecycle-v1")
        self.connections: dict[str, ObservationConnection] = {}
        self.io_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.counters: Counter[str] = Counter()
        self.failed = False
        self.available = False
        self.closing = False

    async def disk(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        # One off-loop operation; cancellation cannot detach an uncertain write
        # and let the next snapshot race it. No per-event broker task is spawned.
        async def perform() -> Any:
            async with self.io_lock:
                return await asyncio.to_thread(method, *args, **kwargs)

        work = asyncio.create_task(perform())
        cancelled = None
        try:
            while True:
                try:
                    result = await asyncio.shield(work)
                    break
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                    if work.cancelled():
                        raise
            if cancelled is not None:
                raise cancelled
            return result
        except OwnershipError:
            # Expected capacity/replay fences reject this new connection;
            # existing rows still have reserved capacity for disconnection.
            raise
        except Exception:
            self.failed = True
            self.counters["storage_failed"] += 1
            raise OwnershipError("Observation storage unavailable.") from None

    async def start(self) -> None:
        await self.disk(self.store.start, self.config.signer.producer)
        self.task = asyncio.create_task(self.run(), name="gateway-lifecycle-publisher")

    async def open(
        self, claims: dict[str, Any], owner_generation: int, trace: str
    ) -> ObservationConnection:
        if self.failed or self.closing:
            raise OwnershipError("Observation publisher unavailable.")
        stream = f"{claims['participant_session_id']}:{claims['generation']}:{owner_generation}"
        lobby = "lobby" in claims["scopes"]
        body = envelope(
            {
                "schema_version": 1,
                "meeting_id": claims["meeting_id"],
                "participant_id": claims["participant_id"],
                "participant_session_id": claims["participant_session_id"],
                "generation": claims["generation"],
                "gateway_owner_id": self.config.signer.producer,
                "gateway_generation": owner_generation,
                "authorization_epoch": claims["authorization_epoch"],
                "sequence": 0,
                "trace_id": trace,
            },
            "participant.lobby_connected" if lobby else "participant.signaling_connected",
            snapshot("LOBBY_CONNECTED" if lobby else "SIGNALING_CONNECTED"),
            {},
        )
        try:
            await self.disk(self.store.write, stream, body, opening=True)
        except asyncio.CancelledError:
            # Cancellation can happen after commit but before the socket owns
            # the returned connection. Reconcile this otherwise orphaned row.
            try:
                await self.disk(
                    self.store.write,
                    stream,
                    envelope(
                        body,
                        "participant.signaling_disconnected",
                        snapshot("OFFLINE"),
                        {"reason_code": "normal"},
                    ),
                )
            finally:
                self.wake.set()
            raise
        connection = ObservationConnection(self, stream, body)
        self.connections[stream] = connection
        self.wake.set()
        return connection

    async def lost(self) -> None:
        for connection in tuple(self.connections.values()):
            if connection.body["snapshot"]["connection"] != "LOBBY_CONNECTED":
                await connection.close(lost=True)

    async def relay(self) -> None:
        for stream, body in await self.disk(self.store.pending):
            await self.transport.publish(
                LIFECYCLE, self.config.signer.sign(body, "lifecycle-event"), body["event_id"]
            )
            await self.disk(self.store.delivered, stream, body)
            self.counters["published"] += 1

    async def run(self) -> None:
        reconcile_at = asyncio.get_running_loop().time() + 300
        while not self.closing:
            try:
                await self.transport.start()
                while not self.closing:
                    self.wake.clear()
                    if asyncio.get_running_loop().time() >= reconcile_at:
                        await self.disk(self.store.replay)
                        self.counters["reconciliations"] += 1
                        reconcile_at = asyncio.get_running_loop().time() + 300
                    await self.relay()
                    if not await self.transport.healthy():
                        raise OwnershipError("Observation transport unavailable.")
                    self.available = True
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self.wake.wait(), 0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.available = False
                self.counters["retry"] += 1
                await asyncio.sleep(0.5)
            finally:
                with suppress(Exception):
                    await self.transport.close()

    async def aclose(self) -> None:
        self.closing = True
        self.available = False
        try:
            for connection in tuple(self.connections.values()):
                await connection.close()
        finally:
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            await self.disk(self.store.close)
        # Pending snapshots remain durable; shutdown never waits for a broker.
