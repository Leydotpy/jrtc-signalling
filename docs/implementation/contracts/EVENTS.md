# Django↔gateway command/event contracts (version 1 draft)

## Command (Django→gateway)
```json
{
 "schema_version":1,"command_id":"cmd-id","type":"participant.admit",
 "meeting_id":"meeting-id","participant_id":"participant-id",
 "participant_session_id":"session-id","authorization_epoch":7,
 "target_owner":"gateway-worker-or-route-key","expected_owner_generation":3,
 "issued_at":"RFC3339 UTC","expires_at":"RFC3339 UTC",
 "actor_id":"host-id","trace_id":"trace-id","data":{}
}
```
Other commands: `participant.deny`, `participant.disconnect`, `participant.permissions.update`, `meeting.terminate`, `meeting.reconcile`. Commands require durable outbox, bounded retries + DLQ, dedupe command_id, authorization revalidation for sensitive commands, explicit status/ack and outcome events. Route only to current owner; stale epochs/generations rejected. Identity of issuing service is authenticated; broker payload alone is not permission.

## Event (gateway→Django)
```json
{
 "schema_version":1,"event_id":"evt-id","type":"participant.media_connected",
 "meeting_id":"meeting-id","participant_id":"participant-id",
 "participant_session_id":"session-id","gateway_owner_id":"gw-id",
 "gateway_generation":3,"authorization_epoch":7,"sequence":15,
 "occurred_at":"RFC3339 UTC","trace_id":"trace-id","data":{"media_kind":"audio"}
}
```
Other events: `participant.lobby_connected`, `participant.media_connecting`, `participant.media_disconnected`, `participant.signaling_disconnected`, `participant.media_publishing_changed`, `participant.removal_enforced`, `meeting.resources_terminated`, `gateway.session_lost`, `gateway.recovery_failed`.

## Processing rules
1. Validate schema/version, trusted producer, connection ownership, participant/session association, epoch/generation; reject or dead-letter malformed/stale/tampered events.
2. Inbox dedupe by event_id and monotonic per-session `(generation, sequence)`; handle out-of-order delivery, duplicates and delayed old-session disconnections.
3. Never interpret a gateway event as authorizing admission or role promotion. Durable permission changes originate from Django transactions.
4. Update state transactionally, then publish state-notification (transactional outbox or `on_commit` with recovery). SSE message has meeting projection version/participant delta; client hydrates if gap.
5. Broker outage must not block Janus reader/command future. Use bounded queues/spool as appropriate, explicit overflow; critical command/event delivery reliable/replayed with idempotency; high-rate telemetry sampled separately.
6. Event and command status are different. Record requested, accepted, applied, failed, timed_out. Return HTTP 202 for accepted async control where appropriate; never report physical removal solely based on enqueue.
7. Raw `janus.events` handling is compatible but may not replace domain projection event semantics. Locate and reuse existing Broka, JRTC publisher and Django `Dispio` integration; no duplicate conflicting consumers.

## State model
- `admission`: REQUESTED | WAITING | APPROVED | DENIED | REVOKED
- `connection`: OFFLINE | LOBBY_CONNECTED | SIGNALING_CONNECTED | MEDIA_CONNECTING | MEDIA_CONNECTED | RECOVERING
- State names are draft; SG-01 maps to current Django enum values. Preserve distinction approval vs transport connected, disconnected vs removal.
