"""Bounded JSON parsing and schema checks. No JWT verification or permission grant."""

import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

MAX_MESSAGE_BYTES = 128 * 1024
MAX_ICE_PAYLOAD_BYTES = 64 * 1024
MAX_TICKET_TTL_SECONDS = 120
MAX_COMMAND_TTL_SECONDS = 300
SCHEMA_NAMES = frozenset(
    {
        "request",
        "response",
        "notification",
        "command",
        "event",
        "command-outcome",
        "ticket-header",
        "ticket-claims",
        "state-notification",
    }
)


class ContractError(ValueError):
    """Safe to expose; never includes input values, candidate text, or JWTs."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Duplicate JSON fields are not allowed.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ContractError("Non-finite JSON numbers are not allowed.")


def _timestamp(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError("RFC3339 UTC timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("UTC timestamp required")
    return parsed


_formats = FormatChecker()


@_formats.checks("date-time", raises=(ValueError, TypeError))
def _is_utc(value: Any) -> bool:
    if not isinstance(value, str):
        return True  # The type constraint reports this.
    _timestamp(value)
    return True


@lru_cache(maxsize=len(SCHEMA_NAMES))
def _validator(name: str) -> Draft202012Validator:
    if name not in SCHEMA_NAMES:
        raise ValueError("Unknown contract schema.")
    schema = json.loads(
        files("jrtc_signalling").joinpath(f"schemas/v1/{name}.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=_formats)


def _semantics(name: str, message: dict[str, Any]) -> None:
    if name == "ticket-claims":
        if (
            not message["iat"]
            <= message["nbf"]
            < message["exp"]
            <= message["iat"] + MAX_TICKET_TTL_SECONDS
        ):
            raise ContractError("Invalid ticket validity window.")
    elif name == "command":
        duration = (
            _timestamp(message["expires_at"]) - _timestamp(message["issued_at"])
        ).total_seconds()
        if not 0 < duration <= MAX_COMMAND_TTL_SECONDS:
            raise ContractError("Invalid command validity window.")
    elif name == "state-notification":
        if message["projection_version"] != message["previous_version"] + 1:
            raise ContractError("Invalid projection version transition.")
    elif (
        name == "request"
        and message["op"] == "media.trickle"
        and len(json.dumps(message["payload"], ensure_ascii=False).encode()) > MAX_ICE_PAYLOAD_BYTES
    ):
        raise ContractError("ICE payload exceeds the byte limit.")


def decode_message(raw: str | bytes, *, schema: str = "request") -> dict[str, Any]:
    """Decode one full UTF-8 wire message without exposing invalid input.

    Callers still must verify identities, signatures, live grants, owner fencing,
    sequences and negotiation state before dispatch. Schema validity is not auth.
    """
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ContractError("Message exceeds the byte limit.")
        message = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("Invalid or oversized JSON message.") from None
    return validate_message(message, schema=schema)


def validate_message(message: Any, *, schema: str = "request") -> dict[str, Any]:
    """Validate shape and bounded cross-field invariants, without accepting access."""
    validator = _validator(schema)
    try:
        # Check JSON encodability/size for callers using already-decoded objects too.
        if (
            len(json.dumps(message, ensure_ascii=False, allow_nan=False).encode())
            > MAX_MESSAGE_BYTES
        ):
            raise ContractError("Message exceeds the byte limit.")
        if not isinstance(message, dict) or next(validator.iter_errors(message), None) is not None:
            raise ContractError("Message does not match the v1 contract.")
        _semantics(schema, message)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError("Invalid v1 message.") from None
    return message
