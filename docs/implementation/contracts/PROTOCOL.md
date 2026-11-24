# Versioned browser↔gateway signaling protocol (draft contract)

## Transport
- TLS `wss://`, one authenticated connection per **client session**; choose plain ASGI WebSocket with a documented protocol adapter. Existing Socket.IO event naming/payloads are compatibility inputs, not an excuse to embed Django's namespace in the gateway.
- Document supported browser subprotocols/origins, heartbeat/ping, max message size, per-connection limits, idle timeouts, backpressure, error classes. Browser SDK adapter maps old Socket.IO responses to versioned gateway messages while behind feature flag.
- No long-lived credential in URL/query string. Gateway ticket via secure initial handshake or suitable auth header/subprotocol approach supported by browser runtime; avoid leaking in access logs, redirects, telemetry.

## Required message envelope
```json
{
  "version": 1,
  "id": "client-generated-request-id",
  "op": "media.publisher.publish",
  "meeting_id": "uuid-or-domain-id",
  "participant_session_id": "uuid-or-domain-id",
  "generation": 4,
  "payload": {}
}
```
Response: `id`, `ok`, `result` or stable `{code,message,retryable}`, `generation`, `server_time`. The current authoritative binding is set by handshake, not trusted from mutable client fields.

## Operations to inventory + implement
- `session.hello`, `session.resume`, `session.ping` and bounded health/status
- `media.publisher.join`, `media.publisher.publish`, `media.publisher.configure`, `media.publisher.unpublish`
- `media.subscriber.join`, `media.subscriber.start`, `media.subscriber.update`, `media.subscriber.detach`
- `media.trickle` (`candidates` array, `completed` boolean, publisher/subscriber handle), `media.ice_restart`, `media.hangup`
- server notifications: `session.ready`, `media.jsep`, `media.event`, `session.revoked`, `session.reconnect_required`, `error`
- exact enum names remain **provisional** until SG-01 inspects existing `namespace.py`, `signaling.py`, SDK events/tests and approves a compatibility mapping. Never change the client API silently.

## ICE/JSEP invariants
- A batch of N candidates generates one JRTC trickle request containing N candidates wherever library supports it; all candidates from the final batch must arrive before complete-trickle.
- `[] + completed=true`: complete only; `[] + completed=false`: invalid/no-op only if the existing contract explicitly permits; reject ambiguous forms.
- Support legacy singular `candidate` only during documented compatibility window.
- No unsafe JS number conversion of Janus IDs; browser wire uses decimal strings.
- Correlated JSEP in command response and async Janus event must be deduplicated. Independent unsolicited JSEP must still reach correct client.
- Per-session ordering, bounded in-flight requests and correlation, cancellation on disconnect and timeout, backpressure controls.

## Client continuity
Map existing `synq.js` calls to new transport behind a feature flag (SDK adapter); preserve meeting UI state behavior, participant sounds semantics, ICE batching, offer/answer ordering, and support both paths for canary/rollback. Existing legacy Socket.IO flow remains until validated.
