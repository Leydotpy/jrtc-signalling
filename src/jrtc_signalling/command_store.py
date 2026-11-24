"""Durable gateway command inbox and outcome outbox, independent of Django.

A crash after reservation has an uncertain outcome: never run that command again.
The next owner must reconcile resources instead of replaying a Janus mutation.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .bus import canonical, digest
from .contracts import validate_message


class StoreFull(RuntimeError):
    pass


def outcome(command: dict[str, Any], status: str, reason: str, now: float) -> dict[str, Any]:
    return validate_message(
        {
            "schema_version": 1,
            "type": "command.outcome",
            "event_id": str(uuid5(NAMESPACE_URL, command["command_id"] + ":" + status)),
            "command_id": command["command_id"],
            "meeting_id": command["meeting_id"],
            "gateway_owner_id": command["target_owner"],
            "gateway_generation": command["expected_owner_generation"],
            "authorization_epoch": command["authorization_epoch"],
            "trace_id": command["trace_id"],
            "occurred_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "status": status,
            "reason_code": reason,
        },
        schema="command-outcome",
    )


class CommandStore:
    def __init__(self, path: str, *, capacity: int = 10000) -> None:
        if path == ":memory:" or capacity < 1:
            raise ValueError("Persistent command inbox required.")
        self.path, self.capacity = path, capacity

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            with connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, deadline REAL NOT NULL, expires REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, command_id TEXT NOT NULL REFERENCES commands(id) ON DELETE CASCADE, body TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS pending_outcomes ON outcomes(delivered,next_at)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS quarantine (digest TEXT PRIMARY KEY, reason TEXT NOT NULL, seen_at REAL NOT NULL)"
            )

    @staticmethod
    def _finish(
        db: sqlite3.Connection, command: dict[str, Any], status: str, reason: str, now: float
    ) -> None:
        event = outcome(command, status, reason, now)
        db.execute("UPDATE commands SET state='terminal' WHERE id=?", (command["command_id"],))
        db.execute(
            "INSERT OR IGNORE INTO outcomes(id,command_id,body) VALUES(?,?,?)",
            (event["event_id"], command["command_id"], canonical(event).decode()),
        )

    def begin(self, command: dict[str, Any], now: float) -> str:
        body = canonical(command)
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM commands WHERE id=?", (command["command_id"],)
            ).fetchone()
            if row:
                if row["digest"] != digest(body):
                    return "conflict"
                if row["state"] == "running":
                    if row["deadline"] > now:
                        return "busy"
                    self._finish(db, command, "timed_out", "owner_lost", now)
                # Re-publish persisted outcomes if a prior publication/ACK was lost.
                db.execute(
                    "UPDATE outcomes SET delivered=0,next_at=?,attempts=0 WHERE command_id=?",
                    (now, command["command_id"]),
                )
                return "duplicate"
            if db.execute("SELECT count(*) FROM commands").fetchone()[0] >= self.capacity:
                raise StoreFull("Command inbox capacity reached.")
            expires = datetime.fromisoformat(
                command["expires_at"].replace("Z", "+00:00")
            ).timestamp()
            db.execute(
                "INSERT INTO commands VALUES(?,?,?,?,?,?)",
                (command["command_id"], digest(body), body.decode(), "running", now + 5, expires),
            )
            event = outcome(command, "accepted", "requested", now)
            db.execute(
                "INSERT INTO outcomes(id,command_id,body) VALUES(?,?,?)",
                (event["event_id"], command["command_id"], canonical(event).decode()),
            )
            return "new"

    def finish(self, command: dict[str, Any], status: str, reason: str, now: float) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM commands WHERE id=?", (command["command_id"],)
            ).fetchone()
            if row and row["state"] == "running":
                self._finish(db, command, status, reason, now)

    def maintain(self, now: float) -> None:
        with self.connect() as db:
            rows = db.execute(
                "SELECT body FROM commands WHERE state='running' AND deadline<=? LIMIT 100", (now,)
            ).fetchall()
            for row in rows:
                self._finish(db, json.loads(row["body"]), "timed_out", "owner_lost", now)
            # More than two maximum command TTLs retained after expiry. In-flight
            # and unpublished outcomes are never removed to free capacity.
            db.execute(
                "DELETE FROM commands WHERE id IN (SELECT id FROM commands WHERE state='terminal' AND expires<? AND NOT EXISTS (SELECT 1 FROM outcomes WHERE command_id=commands.id AND delivered!=1) LIMIT 100)",
                (now - 600,),
            )

    def pending(self, now: float) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM outcomes WHERE delivered=0 AND next_at<=? ORDER BY rowid LIMIT 20",
                    (now,),
                )
            ]

    def published(self, event_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE outcomes SET delivered=1 WHERE id=?", (event_id,))

    def failed_publish(self, event_id: str, now: float) -> None:
        with self.connect() as db:
            row = db.execute("SELECT attempts FROM outcomes WHERE id=?", (event_id,)).fetchone()
            attempts = row["attempts"] + 1
            # delivered=2 is a retained dead letter, never a success. Duplicate
            # command delivery reopens it; operators can also replay explicitly.
            db.execute(
                "UPDATE outcomes SET attempts=?,next_at=?,delivered=? WHERE id=?",
                (
                    attempts,
                    now + min(30, 2 ** min(attempts, 5)),
                    2 if attempts >= 10 else 0,
                    event_id,
                ),
            )

    def quarantine(self, packet_digest: str, reason: str, now: float) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO quarantine VALUES(?,?,?)", (packet_digest, reason, now)
            )
            db.execute(
                "DELETE FROM quarantine WHERE digest IN (SELECT digest FROM quarantine ORDER BY seen_at DESC LIMIT -1 OFFSET 1000)"
            )

    def inspect(self) -> dict[str, int]:
        with self.connect() as db:
            return {
                "inbox": db.execute("SELECT count(*) FROM commands").fetchone()[0],
                "pending_outcomes": db.execute(
                    "SELECT count(*) FROM outcomes WHERE delivered=0"
                ).fetchone()[0],
                "dead_outcomes": db.execute(
                    "SELECT count(*) FROM outcomes WHERE delivered=2"
                ).fetchone()[0],
                "quarantined": db.execute("SELECT count(*) FROM quarantine").fetchone()[0],
            }
