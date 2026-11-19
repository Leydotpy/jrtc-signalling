"""Allowlisted structured logs, including server handshake error logs.

Uvicorn logs denied WebSocket URLs via uvicorn.error even with access_log=False.
Never format free-form record messages, arguments, request paths or tracebacks.
Only keyed correlation references on verified session events may be included.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from .telemetry import trace_ref

SESSION_EVENTS = frozenset({"gateway.admitted", "gateway.resumed"})
EVENTS = (
    frozenset(
        {
            "gateway.starting",
            "gateway.started",
            "gateway.draining",
            "gateway.stopped",
            "gateway.failed",
        }
    )
    | SESSION_EVENTS
)


def trace_session(key: bytes, claims: dict[str, Any], *, resume: bool) -> None:
    if key:
        event = "gateway.resumed" if resume else "gateway.admitted"
        logging.getLogger("jrtc_signalling.session").info(
            event,
            extra={
                "gateway_event": event,
                "meeting_ref": trace_ref(key, "meeting", claims["meeting_id"]),
                "session_ref": trace_ref(key, "session", claims["participant_session_id"]),
            },
        )


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "gateway_event", "server.log")
        if not isinstance(event, str) or event not in EVENTS:
            event = "server.log"
        fields = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": event,
        }
        if event in SESSION_EVENTS:
            for field in ("meeting_ref", "session_ref"):
                value = getattr(record, field, None)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
                    fields[field] = value
        return json.dumps(fields)


def log_config(level: str) -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"safe": {"()": SafeJSONFormatter}},
        "handlers": {
            "safe": {
                "class": "logging.StreamHandler",
                "formatter": "safe",
                "stream": "ext://sys.stderr",
            }
        },
        "root": {"handlers": ["safe"], "level": level.upper()},
        "loggers": {
            name: {"handlers": ["safe"], "level": level.upper(), "propagate": False}
            for name in (
                "uvicorn",
                "uvicorn.error",
                "uvicorn.access",
                "websockets",
                "jrtc_signalling",
            )
        },
    }
