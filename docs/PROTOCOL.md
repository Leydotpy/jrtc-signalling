# Signalling v1 contract

Status: SG-01 contract baseline. Shapes and bounded parsing are implemented;
operations, cryptographic verification, Broka delivery and SSE are subsequent
tasks. This document and the exported schemas supersede the imported draft for
this service. They do not change any current SDK method or Django endpoint.

Canonical definitions: `src/jrtc_signalling/schema_definitions.py`. Language-neutral
JSON Schema 2020-12: `src/jrtc_signalling/schemas/v1/`. Synthetic examples for every
operation/command/event: `tests/fixtures/v1.json`. Run
`uv run python scripts/export_schemas.py --check` to detect drift. Python callers
use `decode_message`; non-Python consumers must also enforce the documented byte
and time-window constraints, which JSON Schema cannot express.

## Transport and binding

- Native ASGI WebSocket at configurable `/ws`, subprotocol `jrtc.signalling.v1`.
  TLS is mandatory at the production ingress. Exact origin allowlisting is part
  of SG-04. No JWT in URLs, cookies or subprotocols; first message is
  `session.hello` carrying a Django-issued short-lived ticket.
- `meeting_id` means the existing **MeetingSession.pk**, not MeetingRoom.pk.
  `participant_session_id` is a Django-issued connection/grant session identity;
  it is never the caller's unchecked `client_session_key` or Socket.IO sid.
  Envelope bindings must equal verified ticket and current authoritative grant.
- `generation` identifies the Django-authorized client connection generation.
  `gateway_generation` is the owner lease's fencing token and is a separate
  counter. A process UUID is diagnostic and cannot establish either fence.
- Each direction has its own increasing `sequence`, starting at 1 in a new
  client generation. Request `id` correlates results. Dispatch state-changing
  operations in order per session; bounded duplicate IDs return an already-known
  outcome, never repeat a Janus mutation. Gaps reject with `OUT_OF_ORDER`.
- `negotiation_id` identifies an ICE/JSEP exchange within one client generation.
  Publisher supplies a fresh ID with its offer; gateway binds it after authority
  checks. Gateway allocates subscriber IDs with its offers; start/trickle must
  match. Old callbacks and candidates reject with `STALE_NEGOTIATION`.
- Maximum message: 128 KiB UTF-8; ICE payload: 64 KiB, at most 32 candidates.
  Empty candidates require `completed=true`; booleans are not coerced. v1 uses
  only plural `candidates`. The SG-12 legacy adapter may normalize a singular
  candidate before this boundary. Never accept both forms together.
- Proposed SG-04 budgets: authenticate in 5 s, 16 in-flight requests, 32 queued
  replies, 20 commands/s with burst 40, idle timeout 90 s, ping interval 25 s.
  Overload rejects/closes; queues and deduplication stores must be bounded.
  The scaffold enforces server frame/queue/ping limits only; it rejects all WS
  upgrades until auth and runtime are implemented.

## Compatibility map

Source paths and exact SHAs are recorded in [SG-00](implementation/tracking/reports/SG-00.md).
This is the map for the future SDK transport adapter, not a replacement SDK.

| Current MeetingConnection call / Socket.IO event | v1 gateway operation | Payload and response |
| --- | --- | --- |
| connectUser / connect | session.hello | ticket from Django HTTPS; no reused Socket.IO authentication |
| subscribeToSession / session_subscribe | Django state + session.hello | existing SessionStatePayload remains authoritative |
| publishMedia / session_media_publish | media.publisher.publish | preserve offer and tracks; initial join-and-configure, then publish/configure according to live state |
| publishMedia on an active handle | media.publisher.configure (or publish) | same offer/tracks and MediaSignalAck |
| unpublishMedia / session_media_unpublish | media.publisher.unpublish | empty payload; retain publisher handle as current semantics require |
| syncSubscriptions / session_media_sync_subscriptions | media.subscriber.sync | gateway derives allowed publishers from trusted state, then joins or updates; returns offer/layout |
| startSubscriber / session_media_start_subscriber | media.subscriber.start | answer plus negotiation_id |
| trickle / session_media_trickle | media.trickle | handle_type, candidates, completed plus negotiation_id; preserve final batch |
| heartbeat / session_heartbeat | session.ping plus Django presence projection | transport heartbeat is not an admission grant |
| reviewJoinRequest / session_review_join_request | Django HTTPS control | no browser gateway admission operation |
| updateParticipant, removeParticipant, endSession | Django HTTPS control + durable command | enqueue is accepted, transport enforcement is separately confirmed |
| requestAdmission, createJoinRequest, getSessionState | Django HTTPS | retain existing admission/join-request/state APIs |
| leaveSession / session_leave | Django HTTPS then bounded gateway cleanup | permission/intent and physical detach remain distinct |
| chat, reactions, session_state | Django application channel | do not send application state through the media gateway |
| reconnect | session.resume with new Django ticket | new generation and owner/grant validation; old handles are not inherited by default |

Publisher.join, subscriber.join/update/detach, ice_restart and hangup are explicit
low-level v1 operations. They are not new public SDK calls. TextRoom is excluded
from v1; existing chat stays with Django. Subscriber targets are server-derived;
browser-supplied Janus room/feed IDs are not authorization.

Successful response: version, request id, ok=true, generation, UTC server_time,
and result. A media result retains action, participant_id, handle_type,
lifecycle_state, jsep, streams and selected_streams; negotiation_id is additive.
The selected-stream fields feed, mid, crossrefid, sub_mid preserve existing null
values. Janus IDs are positive canonical decimal **strings**, including values
larger than JavaScript's safe integer range. Convert explicitly to Python ints
only at the internal JRTC boundary. Metadata needs separate redaction before
any application log or domain event.

## Janus/JSEP event map

| Janus signal | Browser delivery | Domain observation |
| --- | --- | --- |
| transaction-correlated JSEP result | request result, once | no SDP on Broka |
| same transaction through async callback | suppress duplicate negotiation; reconcile layout separately | no second media-connected grant |
| unsolicited JSEP | media.jsep with new event_id and bound negotiation_id | none containing SDP |
| webrtcup | media.event | participant.media_connected |
| media availability change | media.event | participant.media_publishing_changed |
| joining/publishers/leaving/unpublished | topology_changed, then permitted subscriber.sync | bounded state observation |
| hangup/detached/session loss | media.event or session.reconnect_required | disconnect/session_lost observation |
| revocation enforced | session.revoked and close | participant.removal_enforced + command outcome |

Deduplicate by owner/client/negotiation binding and transaction/event identity.
Unsolicited JSEP remains deliverable; do not suppress every event that happens
to have a transaction field. A final ICE batch is one JRTC trickle call with the
whole sequence, followed by complete_trickle. Never send completion first.

## Errors, timeout and recovery

Errors carry stable code, safe message and retryable; they never echo ticket,
SDP, ICE, library exception text or broker payloads. Codes are enumerated in the
response schema. `OUTCOME_UNKNOWN` after a state-changing send is not retryable;
reconcile or replace the fenced generation instead of replaying the operation.
Unknown schema versions reject before dispatch. Required fields or semantics
that change incompatibly require a new version/subprotocol.

Close policies for SG-04: 1008 policy denial, 1009 oversized message, 1013 temporary
overload/unavailable, 1012 drain/restart. Before accepting a WebSocket, use a safe
HTTP denial when the ASGI server supports it. No detailed auth errors in logs.

## Commands, observations and projection

Separate streams: `synq.signalling.commands.v1` and
`synq.signalling.events.v1`, alongside the unchanged legacy `janus.events`.
Use authenticated Broka Redis Streams with consumer-group ACK/reclaim, Django
transactional outbox/inbox, retries and DLQ. Delivery is at least once;
semantic deduplication makes application idempotent, not transport exactly once.
No event callback may await broker or database work on the Janus receive path.

Commands require owner targeting, expected owner generation, authorization epoch,
expiry (1–300 seconds after issuance), actor, trace and command ID. Validate
trusted producer identity independently from payload fields. Stale commands
cannot restore permission. Meeting-wide commands have null participant/session
IDs and a meeting-wide authorization epoch. Participant commands bind both IDs.
Expired or ambiguous enforcement must reconcile current Django grants.

Domain events permit only their typed data; no arbitrary metadata, SDP, ICE or
credentials. Consumers verify owner, grant and session association, then dedupe
event_id and monotonic (gateway_generation, sequence) within the bound session.
Old-session disconnects cannot roll back new-session presence. Separate
`command.outcome` statuses are accepted/applied/failed/timed_out. Only applied
confirms physical enforcement; a lost ACK remains an unknown outcome.

Existing state mapping:

| Django state | Admission meaning | Observation meaning |
| --- | --- | --- |
| join request pending / participant waiting | WAITING | may be offline or lobby-connected |
| join request admitted / participant admitted | APPROVED | media connection is not implied |
| participant active | must still consult authoritative grant | projected active transport |
| join request rejected | DENIED | no media allowed |
| participant removed | REVOKED | enforcement can still be pending |
| disconnected / stale / left | no new admission decision | connection/lifecycle observation |

SSE design: `GET /api/v1/meetings/sessions/{id}/events/` (planned SG-11), event
`state.changed`, Last-Event-ID replay and committed projection versions.
Notifications invalidate affected participants; the client refreshes the existing
`sessions/{id}/state/` snapshot into the existing store. A gap, expired replay or
generation change forces a snapshot. Initial/replay/snapshot contexts remain
silent for sounds. An SSE event never independently grants admission.
