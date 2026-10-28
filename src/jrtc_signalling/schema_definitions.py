"""Canonical v1 wire definitions; export with scripts/export_schemas.py.

These schemas describe messages, not authorization. In particular, validating
ticket claims never verifies a JWT or grants access.
"""

from typing import Any

type Schema = dict[str, Any]
SAFE_INT = 2**53 - 1
ID: Schema = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
}
COUNTER: Schema = {"type": "integer", "minimum": 0, "maximum": SAFE_INT}
SEQUENCE: Schema = {**COUNTER, "minimum": 1}
TIME: Schema = {"type": "string", "format": "date-time", "maxLength": 40}
BOOL: Schema = {"type": "boolean"}
HANDLE: Schema = {"enum": ["publisher", "subscriber"]}
JANUS_ID: Schema = {"type": "string", "pattern": "^[1-9][0-9]*$", "maxLength": 128}
SCOPES: Schema = {
    "type": "array",
    "minItems": 1,
    "maxItems": 4,
    "uniqueItems": True,
    "items": {"enum": ["lobby", "media:publish", "media:subscribe", "session:resume"]},
}
REASON: Schema = {
    "enum": [
        "requested",
        "denied",
        "revoked",
        "meeting_ended",
        "transport_lost",
        "expired",
        "owner_lost",
        "permission_changed",
        "unavailable",
        "timeout",
        "normal",
    ]
}


def obj(properties: Schema, required: list[str] | None = None, **extra: Any) -> Schema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
        **extra,
    }


def array(items: Schema, maximum: int, *, minimum: int = 0) -> Schema:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def text(maximum: int, *, minimum: int = 0) -> Schema:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def nullable(schema: Schema) -> Schema:
    return {"anyOf": [schema, {"type": "null"}]}


def document(name: str, schema: Schema) -> Schema:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:jrtc-signalling:v1:{name}",
        "title": name,
        **schema,
    }


def jsep(kind: str | None = None) -> Schema:
    return obj(
        {
            "type": {"const": kind} if kind else {"enum": ["offer", "answer"]},
            "sdp": text(65536, minimum=1),
        }
    )


TRACK = obj(
    {
        "source": {"enum": ["microphone", "camera", "screen_share", "screen_share_audio"]},
        "kind": {"enum": ["audio", "video"]},
        "mid": text(128),
        "label": text(256),
        "track_id": text(128),
        "stream_id": text(128),
    },
    ["source", "kind"],
)
CANDIDATE = obj(
    {
        "candidate": text(4096, minimum=1),
        "sdpMid": nullable(text(128)),
        "sdpMLineIndex": nullable({"type": "integer", "minimum": 0, "maximum": 65535}),
        "usernameFragment": nullable(text(256)),
    },
    ["candidate"],
    anyOf=[
        {"required": ["sdpMid"], "properties": {"sdpMid": text(128, minimum=1)}},
        {
            "required": ["sdpMLineIndex"],
            "properties": {"sdpMLineIndex": {"type": "integer", "minimum": 0}},
        },
    ],
)
ICE = obj(
    {
        "handle_type": HANDLE,
        "negotiation_id": ID,
        "candidates": array(CANDIDATE, 32),
        "completed": BOOL,
    },
    anyOf=[
        {"properties": {"candidates": {"minItems": 1}}},
        {"properties": {"completed": {"const": True}}},
    ],
)
PUBLISH = obj({"offer": jsep("offer"), "tracks": array(TRACK, 16), "negotiation_id": ID})
EMPTY = obj({})
REQUEST_PAYLOADS: dict[str, Schema] = {
    "session.hello": obj({"ticket": text(8192, minimum=1)}),
    "session.resume": obj({"ticket": text(8192, minimum=1), "last_sequence": COUNTER}),
    "session.refresh": obj({"ticket": text(8192, minimum=1)}),
    "session.ping": EMPTY,
    "media.publisher.join": EMPTY,
    "media.publisher.publish": PUBLISH,
    "media.publisher.configure": PUBLISH,
    "media.publisher.unpublish": EMPTY,
    "media.subscriber.join": EMPTY,
    "media.subscriber.sync": EMPTY,
    "media.subscriber.update": EMPTY,
    "media.subscriber.start": obj({"answer": jsep("answer"), "negotiation_id": ID}),
    "media.subscriber.detach": EMPTY,
    "media.trickle": ICE,
    "media.ice_restart": obj({"handle_type": HANDLE}),
    "media.hangup": obj({"handle_type": HANDLE}),
}
REQUEST = obj(
    {
        "version": {"type": "integer", "const": 1},
        "id": ID,
        "op": {"enum": list(REQUEST_PAYLOADS)},
        "meeting_id": ID,
        "participant_session_id": ID,
        "generation": SEQUENCE,
        "sequence": SEQUENCE,
        "payload": {},
    },
    oneOf=[
        {"properties": {"op": {"const": op}, "payload": payload}}
        for op, payload in REQUEST_PAYLOADS.items()
    ],
)

# Keep the browser MediaSignalAck fields. Janus identifiers always stay strings.
STREAM = obj(
    {
        "id": ID,
        "direction": {"enum": ["inbound", "outbound"]},
        "media_kind": {"enum": ["audio", "video", "screen", "data"]},
        "janus_mid": text(128),
        "janus_feed_id": nullable(JANUS_ID),
        "janus_feed_mid": text(128),
        "codec": text(128),
        "is_active": BOOL,
        "is_ready": BOOL,
        "is_moderated": BOOL,
        "metadata": {"type": "object", "maxProperties": 32},
        "source_participant_id": nullable(ID),
    },
    [
        "id",
        "direction",
        "media_kind",
        "janus_mid",
        "janus_feed_id",
        "janus_feed_mid",
        "is_active",
        "is_ready",
        "is_moderated",
        "metadata",
    ],
)
SELECTED = obj(
    {
        "mid": nullable(text(128)),
        "feed": nullable(JANUS_ID),
        "crossrefid": nullable(text(128)),
        "sub_mid": nullable(text(128)),
    }
)
MEDIA_ACK = obj(
    {
        "action": {
            "enum": [
                "join_and_configure",
                "publish",
                "configure",
                "unpublish",
                "join",
                "update",
                "start",
                "trickle",
                "noop",
                "detach",
                "hangup",
                "ice_restart",
            ]
        },
        "participant_id": ID,
        "handle_type": HANDLE,
        "lifecycle_state": {
            "enum": ["attaching", "attached", "joining", "ready", "detaching", "detached", "failed"]
        },
        "jsep": nullable(jsep()),
        "streams": array(STREAM, 256),
        "selected_streams": array(SELECTED, 256),
        "negotiation_id": ID,
    },
    ["action", "participant_id", "handle_type", "lifecycle_state", "streams", "selected_streams"],
)
READY = obj(
    {
        "participant_id": ID,
        "scopes": SCOPES,
        "authorization_epoch": COUNTER,
        "gateway_owner_id": ID,
        "gateway_generation": SEQUENCE,
    }
)
ERROR = obj(
    {
        "code": {
            "enum": [
                "INVALID_MESSAGE",
                "UNSUPPORTED_VERSION",
                "UNAUTHENTICATED",
                "FORBIDDEN",
                "EXPIRED_TICKET",
                "REPLAYED_TICKET",
                "STALE_GENERATION",
                "STALE_AUTHORIZATION",
                "STALE_NEGOTIATION",
                "OUT_OF_ORDER",
                "RATE_LIMITED",
                "OVERLOADED",
                "UNAVAILABLE",
                "TIMEOUT",
                "OUTCOME_UNKNOWN",
                "NOT_IMPLEMENTED",
            ]
        },
        "message": text(256, minimum=1),
        "retryable": BOOL,
    }
)
RESPONSE = obj(
    {
        "version": {"type": "integer", "const": 1},
        "id": ID,
        "ok": BOOL,
        "generation": SEQUENCE,
        "server_time": TIME,
        "result": {"oneOf": [MEDIA_ACK, READY, EMPTY]},
        "error": ERROR,
    },
    ["version", "id", "ok", "generation", "server_time"],
    oneOf=[
        {
            "properties": {"ok": {"const": True}},
            "required": ["result"],
            "not": {"required": ["error"]},
        },
        {
            "properties": {"ok": {"const": False}},
            "required": ["error"],
            "not": {"required": ["result"]},
        },
    ],
)
NOTIFICATION_PAYLOADS = {
    "session.ready": READY,
    "session.revoked": obj({"reason_code": REASON}),
    "session.reconnect_required": obj({"reason_code": REASON}),
    "media.jsep": obj(
        {"handle_type": HANDLE, "negotiation_id": ID, "jsep": jsep(), "transaction": nullable(ID)}
    ),
    "media.event": obj(
        {
            "handle_type": HANDLE,
            "kind": {"enum": ["webrtcup", "media", "hangup", "detached", "topology_changed"]},
            "janus_handle_id": JANUS_ID,
        }
    ),
    "error": ERROR,
}
NOTIFICATION = obj(
    {
        "version": {"type": "integer", "const": 1},
        "event_id": ID,
        "op": {"enum": list(NOTIFICATION_PAYLOADS)},
        "meeting_id": ID,
        "participant_session_id": ID,
        "generation": SEQUENCE,
        "sequence": SEQUENCE,
        "server_time": TIME,
        "payload": {},
    },
    oneOf=[
        {"properties": {"op": {"const": op}, "payload": payload}}
        for op, payload in NOTIFICATION_PAYLOADS.items()
    ],
)

PERMISSIONS = obj(
    {
        "can_publish_audio": BOOL,
        "can_publish_video": BOOL,
        "can_share_screen": BOOL,
        "can_subscribe": BOOL,
    }
)
COMMAND_DATA = {
    "participant.admit": obj({"grant_id": ID, "scopes": SCOPES, "permissions": PERMISSIONS}),
    "participant.deny": obj({"reason_code": REASON}),
    "participant.disconnect": obj({"reason_code": REASON}),
    "participant.permissions.update": obj(
        {"grant_id": ID, "scopes": SCOPES, "permissions": PERMISSIONS}
    ),
    "meeting.terminate": obj({"reason_code": REASON}),
    "meeting.reconcile": obj({"projection_version": COUNTER}),
}


def domain_message(kind: str, variants: dict[str, Schema]) -> Schema:
    props = {
        "schema_version": {"type": "integer", "const": 1},
        f"{kind}_id": ID,
        "type": {"enum": list(variants)},
        "meeting_id": ID,
        "participant_id": nullable(ID),
        "participant_session_id": nullable(ID),
        "authorization_epoch": COUNTER,
        "trace_id": ID,
        "data": {},
    }
    if kind == "command":
        props.update(
            target_owner=ID,
            expected_owner_generation=SEQUENCE,
            issued_at=TIME,
            expires_at=TIME,
            actor_id=ID,
        )
    else:
        props.update(
            gateway_owner_id=ID, gateway_generation=SEQUENCE, occurred_at=TIME, sequence=SEQUENCE
        )
    choices = []
    for name, data in variants.items():
        identities = {"type": "null"} if name.startswith("meeting.") else ID
        choices.append(
            {
                "properties": {
                    "type": {"const": name},
                    "data": data,
                    "participant_id": identities,
                    "participant_session_id": identities,
                }
            }
        )
    return obj(props, oneOf=choices)


EVENT_DATA = {
    "participant.lobby_connected": EMPTY,
    "participant.media_connecting": obj({"handle_type": HANDLE}),
    "participant.media_connected": obj(
        {"media_kind": {"enum": ["audio", "video", "screen", "data"]}}
    ),
    "participant.media_disconnected": obj({"reason_code": REASON}),
    "participant.signaling_disconnected": obj({"reason_code": REASON}),
    "participant.media_publishing_changed": obj({"audio": BOOL, "video": BOOL, "screen": BOOL}),
    "participant.removal_enforced": obj({"command_id": ID}),
    "meeting.resources_terminated": obj({"command_id": ID}),
    "gateway.session_lost": obj({"reason_code": REASON}),
    "gateway.recovery_failed": obj({"reason_code": REASON}),
}
COMMAND_OUTCOME = obj(
    {
        "schema_version": {"type": "integer", "const": 1},
        "event_id": ID,
        "type": {"const": "command.outcome"},
        "command_id": ID,
        "meeting_id": ID,
        "gateway_owner_id": ID,
        "gateway_generation": SEQUENCE,
        "authorization_epoch": COUNTER,
        "occurred_at": TIME,
        "trace_id": ID,
        "status": {"enum": ["accepted", "applied", "failed", "timed_out"]},
        "reason_code": REASON,
    }
)
TICKET = obj(
    {
        "iss": text(256, minimum=1),
        "aud": {"const": "jrtc-signalling"},
        "sub": ID,
        "jti": ID,
        "iat": COUNTER,
        "nbf": COUNTER,
        "exp": SEQUENCE,
        "meeting_id": ID,
        "participant_id": ID,
        "participant_session_id": ID,
        "generation": SEQUENCE,
        "authorization_epoch": COUNTER,
        "scopes": SCOPES,
        "grant_id": ID,
        "gateway_route": ID,
    },
    [
        "iss",
        "aud",
        "sub",
        "jti",
        "iat",
        "nbf",
        "exp",
        "meeting_id",
        "participant_id",
        "participant_session_id",
        "generation",
        "authorization_epoch",
        "scopes",
        "grant_id",
    ],
    allOf=[
        {
            "if": {"properties": {"scopes": {"contains": {"const": "lobby"}}}},
            "then": {"properties": {"scopes": {"items": {"enum": ["lobby", "session:resume"]}}}},
        }
    ],
)
TICKET_HEADER = obj({"alg": {"const": "RS256"}, "typ": {"const": "JWT"}, "kid": ID})
STATE_NOTIFICATION = obj(
    {
        "schema_version": {"type": "integer", "const": 1},
        "event_id": ID,
        "type": {"const": "state.changed"},
        "meeting_id": ID,
        "projection_version": SEQUENCE,
        "previous_version": COUNTER,
        "participant_ids": array(ID, 256),
        "occurred_at": TIME,
        "context": {"enum": ["live", "replay", "snapshot"]},
    }
)

SCHEMAS = {
    name: document(name, schema)
    for name, schema in {
        "request": REQUEST,
        "response": RESPONSE,
        "notification": NOTIFICATION,
        "command": domain_message("command", COMMAND_DATA),
        "event": domain_message("event", EVENT_DATA),
        "command-outcome": COMMAND_OUTCOME,
        "ticket-claims": TICKET,
        "ticket-header": TICKET_HEADER,
        "state-notification": STATE_NOTIFICATION,
    }.items()
}
