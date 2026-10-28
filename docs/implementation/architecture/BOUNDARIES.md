# Architecture & ownership boundaries

```text
                             HTTPS controls/tickets     Django
Browser synq.js  --------------------------------------> (auth, roles, admission, durable permissions, outbox, projection)
      |     ^                                                | ^
      |     | SSE / app events                               | | Broka durable commands/events
      |     +------------------------------------------------+ |
      |                                                        |
      +---- WebSocket (scoped auth, SDP, ICE, recovery) ----> Signaling gateway (ASGI)
      |                                                        | JRTC + jrtc-video
      +============= WebRTC (DTLS/SRTP/RTCP) ===============> Janus Media Server
```

### Authority
- Django owns: authentication/guests, meeting lifecycle, admission request/approval/denial, bans/removals, roles, permissions, persisted audit, signed credentials/authorization epochs, command outbox, read-side meeting projection.
- Gateway owns: active WebSocket connections, ticket validation, transient per-connection state, JRTC sessions/handle registry, Janus plugin commands, SDP/ICE sequencing, transport recovery, session generation and runtime routing, observed signaling/media events.
- `jrtc` is generic Janus transport/transactions/handles/events; `jrtc-video` implements VideoRoom operations. Do not add Django, meeting roles, or Broka domain schemas to reusable JRTC packages.
- Broka carries control commands and lifecycle observations; no high-frequency candidate trickle through broker. `janus.events` may exist as a legacy raw Janus stream; preserve/bridge it where required but *application domain* events are versioned separately and not blindly treated as proof of admission.
- PostgreSQL: durable permission/membership/meeting state and transactional outbox/inbox. Redis: live read projections, optional lease/routing and SSE fanout. Never assume Redis Pub/Sub alone supplies durable exactly-once delivery.
- Frontend HTTPS Django = identity/controls/snapshots; SSE Django = committed application state; WS gateway = live media signaling; WebRTC browser↔Janus = media.

### Migration architecture
- Pre-cutover: legacy Django Socket.IO + Janus runtime continue servicing existing traffic, while gateway runs in shadow/testing isolated meetings. Do NOT attach same active handle from both.
- New path: gateway owns signaling/JRTC in explicit cohorts or selected meetings; Django ASGI/Celery do not own that cohort's Janus resources. Route commands to proper gateway owner using binding/lease/fencing.
- Rollback: disable new routing for **new** connections, drain gateway ownership, re-establish clean legacy resources only after ownership expires; never reattach stale live handles in two processes.
- Beyond scope: Go/Rust rewrite; new features unrelated to gateway; broad cleanup of historical Django migrations.

### Physical packaging decision
SG-00 must record existing workspaces. Build a standalone `synq-signaling-gateway` Python service (package or repo) and isolate domain types in `contracts` SDK or generated types. Do not import `apps.meetings` or Django settings into gateway, even if the package resides alongside `synq` locally. Prefer a new sibling folder if no repo exists, without creating remote GitHub objects unasked.
