# Gateway lifecycle observations — SG-10

`JRTC_SIGNALLING_LIFECYCLE_EVENTS=false` is the default. Enable only for an
isolated gateway cohort with authentication, the SG-07 fenced runtime, the bus
extra and persistent writable storage. Media still needs `MEDIA_REHEARSAL=true`;
lobby observations work independently. Readiness remains 503.

## Contract and source

The signed `lifecycle-event` envelope travels on
`synq.signalling.lifecycle.v1`. Django uses consumer group `django-lifecycle-v1`.
The existing command outcome route `synq.signalling.events.v1`, generic raw
`janus.events` and its logical dispatcher keep their existing meanings. Lifecycle
snapshots never run that raw dispatcher or duplicate its browser side effects.

The additive [lifecycle schema](../src/jrtc_signalling/schemas/v1/lifecycle-event.schema.json)
preserves the older draft `event` schema. It requires event ID, meeting,
participant, participant session, client grant `generation`, gateway owner and
`gateway_generation`, authorization epoch, sequence, UTC occurrence time and trace
ID. The trace starts at the authenticated hello request. The configured Ed25519
key authenticates the producing owner. Ticket credentials are separate.

Every event contains a complete `snapshot`: connection state plus audio/video/
screen publishing flags. Only actual Janus `webrtcup` establishes MEDIA_CONNECTED;
publishing flags reflect accepted tracks on that ready transport. Lobby, signaling,
media connecting/disconnected, publishing changes, socket disconnect and Janus
loss/recovery failure have typed events. A lost connection must renegotiate through
the existing ownership/grant checks; these events do not implement takeover.
Removal and resource-termination confirmations remain SG-13.

No SDP, candidates, Janus credentials, tickets, arbitrary plugin payloads or
permissions are in this route. These are reconciliation snapshots, never a sound
cue or a record of every intermediate transition. SG-11 owns public projection
and notifications. An observation never admits, promotes or restores a participant.

## Durability, bounds and shutdown

The gateway reserves one SQLite row per connection in `<inbox_path>.observations`.
Mount that file, its journal and `.lock` file together on persistent local storage.
Use one writer per spool/owner; a second writer or another producer identity fails
closed. SQLite FULL commits run serially off the event loop. Janus callbacks only
enqueue their existing bounded local events or set the manager's loss-observer
flag. A separate owned publisher performs broker I/O with a two-second deadline.

The spool has 1,024 rows, at most 4 KiB per snapshot and an 8,192-page database
limit (32 MiB at its default page size). Pending/live rows are never evicted.
Capacity rejects new connections while reserving terminal-state space for existing
ones. Storage failure stops new work; `/health` reports degraded Broka state.
This conservative rehearsal budget is not a production capacity claim.

Backpressure replaces a row with its newest complete snapshot. Sequence gaps
therefore represent reconciled intermediate states, not missing permission actions.
Retries reuse event IDs; a late publish ACK cannot clear a newer pending snapshot.
Each relay reads at most 20 rows. Every five minutes the retained snapshots are
replayed, including terminal snapshots, to repair a dropped final broker entry.
Delivered terminal rows expire after 24 hours; unacknowledged rows do not expire.
Use durable Redis Streams with persistence, no eviction, route-scoped ACLs and
retention that respects consumer pending entries. Retain verification keys while
messages/spools signed by them may still replay. Disk destruction or broker loss
beyond retained snapshots needs operator reconciliation, not guessed connectivity.

Shutdown joins socket cleanup and storage commits, stops Janus producers, then
closes the lifecycle worker. It never waits for broker recovery. Cancellation after
a commit preserves the sequence; a canceled hello persists an offline replacement.
On process restart, formerly live rows become OFFLINE `gateway.recovery_failed`
snapshots before replay. No handles or live connections are resurrected.

## Django consumer

With the matching gateway dependency and migration 0018 installed in Synq, set
`GATEWAY_EVENTS_ENABLED=true`, `JRTC_EVENT_BROKER_ENGINE=redis`,
`JRTC_REDIS_MODE=streams` and the existing `GATEWAY_BUS_CONFIG_FILE`. Run the separate
worker `python src/manage.py run_gateway_events`. Do not start it in Django ASGI.

It verifies the signature/schema, exact owner history/current route, immutable
gateway cohort, participant/session association, grant generation, epoch and
current permissions. Online observations also require a current unreleased lease;
terminal replay can finish after lease expiry/release. Future timestamps, unknown
owners and conflicting event IDs/sequences are rejected. Old sessions/epochs/owners
are recorded as stale. A terminal generation cannot become online again.

Under the meeting's transaction lock it commits the inbox receipt and latest
`GatewaySessionObservation` before ACK. Duplicate/reordered events cannot roll back
state; higher complete snapshots reconcile sequence gaps. At most 256 receipt
samples per participant and 1,000 hash-only poison samples are retained. The latest
snapshot retains its replay high-water mark after receipt pruning. Storage or ACK
failures stay pending in Redis. Consumer counters distinguish recorded, reconciled,
stale, duplicate, quarantined and retrying messages.

The observation remains separate from Participant admission, roles, permissions,
joined_at and legacy connections. Readers in SG-11 must compare its grant/epoch/
owner with current authority; this internal row is not itself authorized presence.

## Verification and rollback

Gateway tests cover schemas, signatures, coalescing, capacity, late ACKs, restart,
duplicate writers, cancellation, storage failure and actual Redis backpressure.
The live Janus suite runs with lifecycle events enabled and verifies loss records.
Synq tests cover ordering, authority fences, poison DLQ, receipt bounds and real
PostgreSQL races. `scripts/check_django_lifecycle.py --synq /path/to/synq` exercises
signed Django tickets and ownership HTTP, real Janus media, Broka/Redis and Django
observations with a broker stall. Run it in Synq's Python 3.14 environment with
this gateway, pytest and aiortc installed, using isolated Redis/Janus/PostgreSQL.

Rollback by disabling the gateway flag after draining sockets, preserving its
spool, then stopping the Django event worker. Keep migration data and compatible
keys for replay. Existing command/raw-event workers and legacy routing remain
available. No merge, deployment, production enablement or SG-11 UI/SSE is included.
