"""Allowlisted structured logs, including server handshake error logs.

Uvicorn logs denied WebSocket URLs via uvicorn.error even with access_log=False.
Never format free-form record messages, arguments, request paths or tracebacks.
SG-16 may add explicitly reviewed metadata without weakening this boundary.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

EVENTS = frozenset(
    {"gateway.starting", "gateway.started", "gateway.draining", "gateway.stopped", "gateway.failed"}
)


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "gateway_event", "server.log")
        if not isinstance(event, str) or event not in EVENTS:
            event = "server.log"
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "event": event,
            }
        )


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
