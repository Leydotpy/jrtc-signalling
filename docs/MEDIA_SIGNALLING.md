# Media signalling rehearsal (SG-08/09)

The gateway can dispatch authenticated browser v1 requests directly to the pinned
JRTC/VideoRoom async SDK. This is an explicit isolated rehearsal, not production
readiness. Existing Synq and synq.js transports are unchanged.

## Enablement and ownership

Install the runtime extra and configure [authentication](AUTHENTICATION.md),
[the command plane](COMMAND_PLANE.md) and [exclusive runtime ownership](RUNTIME_OWNERSHIP.md).
Then explicitly set `JRTC_SIGNALLING_MEDIA_REHEARSAL=true`. The default is `false`;
other spellings are rejected. Authentication and a fenced `JanusRuntime` are
required. Runtime meetings must already belong to Django's gateway cohort.

Browser ingress still requires WSS, the exact allowed Origin and
`jrtc.signalling.v1`. After the signed ticket and current grant are validated,
hello returns the real owner ID and lease generation. Lobby and control-only
connections receive an empty hello result and cannot allocate media handles.
Readiness remains **503**, with `signalling=rehearsal` when opted in. Durable
presence, live command enforcement, browser routing and acceptance gates
are still required before cutover.

One connection owns at most one publisher and one subscriber handle. Handles
cannot be adopted by another connection or reused after SDK/session replacement.
A second active connection for the same meeting/participant is denied; reconnect
takeover is SG-14/15 work. Every SDK operation retains the SG-07 lease, generation,
session identity and cancellation fences.

The rehearsal lazily creates a private, nonpersistent, unrecorded VideoRoom for
each configured meeting, with independent random room ID, PIN and management
secret. Browsers never choose or receive the room credentials. Known room IDs
are retained even when a create reply is lost. Shutdown closes peers and destroys
the rooms before releasing ownership; uncertain cleanup, including cancellation,
withholds a successful lease release. Crash reconciliation remains SG-15.

## Operations and negotiation

| Operation | Behaviour |
| --- | --- |
| `media.publisher.join` | Join without publishing; repeated join is a no-op |
| `media.publisher.publish` | Join-and-configure initially; publish after unpublish; configure while already publishing |
| `media.publisher.configure` | Requires an active publisher; apply a new offer and authorised stream controls |
| `media.publisher.unpublish` | Stop publishing and retain the handle; repeated unpublish is a no-op |
| `media.subscriber.join`, `.sync`, `.update` | Derive local permitted feeds and join/update the subscriber; no targets or unchanged targets can be a no-op |
| `media.subscriber.start` | Apply a receive-only answer bound to the outstanding gateway negotiation ID |
| `media.subscriber.detach` | Dispose the owned subscriber handle |
| `media.hangup` | Dispose the selected owned handle |
| `media.trickle` | Forward one validated candidate array, then completion after its ACK; bind to the negotiated SDP and owned handle |
| `media.ice_restart` | Publisher: reserve an ID for its new offer; subscriber: request a new Janus offer. See [ICE lifecycle](ICE_SIGNALLING.md) |

Publisher offers declare tracks by source/kind/MID. The bounded SDP policy gate
checks every active audio/video media section against those declarations and the
current audio/video/screen grant. Missing or duplicate MIDs, undeclared sections,
kind mismatch, ambiguous inference, data channels and unsupported directions are
rejected. Camera versus screen capture provenance cannot be attested by SDP;
the gateway enforces granted source declarations and stream controls, not capture
device identity. Janus remains the complete SDP protocol implementation.

Only publishers in the same meeting, owned by this gateway, currently authorised
and confirmed by Janus `webrtcup` enter the subscription catalogue. An SDP answer
alone does not make a feed ready. Subscriber payloads cannot contain arbitrary
feed IDs. Stream layout and every active subscriber offer section must match the
selected feeds. Answers cannot add/reorder media or publish on the subscriber
handle. Janus IDs remain Python integers internally and canonical decimal strings
on the wire, including values above JavaScript's safe integer range. Unspecified
selected-stream cross-reference/subscriber MID fields retain null values.

Each publisher negotiation uses a new ID, bounded to 64 per connection; reconnect
when that history is exhausted. Subscriber IDs are allocated by the gateway.
An outstanding subscriber offer must be answered before another sync/update.
Transaction-correlated JSEP is delivered once in its response. The exact
transaction/type/SDP digest suppresses its duplicated callback; an unrelated
transaction-bearing unsolicited offer remains deliverable as `media.jsep`.
Unsolicited SDP is validated against the same handle/layout rules and current or
retired ICE credentials. Subscriber negotiations are also bounded to 64 per handle.

Independent fixed-field `media.event` notifications cover readiness, topology,
media, hangup and detach. Callback handlers only enqueue bounded work and never
await WebSocket, Django or Broka I/O. Intentional unpublish hangups are identified
at receipt so a late event does not tear down a republished handle. Unexpected
hangup, session loss or stale ownership closes the connection for a fresh start.

## Authorization and latency boundary

SG-04's conservative policy is preserved: sensitive operations make a fresh
bounded Django authority preflight, then the validated grant is applied locally
to SDP and the owned Janus handle. Only ping/ICE can use the existing 0.5-second
grant cache. There is **still an authority HTTP round trip on a sensitive
transition**. This task does not claim the eventual zero-authority-RPC latency
target: that optimisation requires the later live revocation/fencing work.

SDP, answers and plugin events travel browser → gateway → async JRTC → Janus;
they are never relayed through Django ORM or Broka. Authority requests carry
verified claims and operation names only. A revocation racing an SDK reply cannot
restore a grant. There is no stale-authority fallback. The SG-13 command consumer
still reports unavailable until physical enforcement is implemented, so this
rehearsal must not be treated as production revocation assurance.

## Bounds and recovery

| Resource | Bound |
| --- | --- |
| Media peers | 128 per worker, within the existing 256 authenticated connection limit |
| Dispatch | One ordered worker per connection; at most 16 active operations globally |
| Pending requests | 8 frames and 256 KiB per connection |
| Pending replies/notifications | 16 frames and 256 KiB, including the frame being sent |
| Completed response replay | 64 records and 256 KiB; hashes only for request contents |
| Janus event queue | 32 per peer; SDK event drops also close access |
| SDP | 64 KiB; at most 16 publisher or 256 subscriber media sections |
| ICE | 32 candidates / 64 KiB per request; 256 candidates / 256 KiB forwarded per ICE generation |
| Admission wait | 6 seconds across queue and global-capacity wait |
| External I/O | Existing 2-second authority deadline; each SDK call at most 3 seconds and no later than lease expiry; WebSocket send 2 seconds |

The admission wait is not a total workflow deadline: a first publish can require
room creation, attachment and publishing as separately bounded SDK calls.
Identical pending requests coalesce; completed duplicates return the exact cached
response without repeating a mutation. A changed duplicate or an old sequence
outside the cache fails closed. Queues never grow without a bound.

Malformed client frames/policy violations close 1008, oversized frames 1009,
overload/unavailable authority/slow writers 1013, and uncertain SDK outcomes,
ownership loss or media shutdown 1012. `OUTCOME_UNKNOWN` responses are not
retryable. Reader, writer, request worker and event/guard tasks are cancelled and
joined on disconnect. Disposed handles are never replayed or double-detached.

`AuthenticatedEndpoint.metrics` records fixed-operation counts, errors, total/max
latency, aggregate queue wait, concurrency and duplicate/overflow counters.
It contains no IDs, URLs, SDP, tickets or ICE. Public metric export is SG-16.
Use the supplied CLI and its safe log formatter; do not enable SDK debug output.

## Verification and limits

Wire-level mock Janus tests use the real SDK and ASGI endpoint for correlation,
publisher/subscriber flows, permissions, event delivery, cancellation, resource
bounds and uncertainty. The opt-in real service suite additionally uses Janus,
Redis and two aiortc peers. It checks embedded candidates, direct trickle and
TURN-only trickle, receives actual audio through Janus, and restarts both roles
with fresh credentials and authenticated ICE checks on the same Janus handles.
aiortc is a test-only dependency; the gateway never transports media.

```sh
uv sync --locked --group dev --group media-test --extra runtime
GATEWAY_TEST_JANUS=1 GATEWAY_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
  uv run --no-sync pytest -q tests/test_janus_live.py
```

The suite requires Janus, its VideoRoom/WebSocket modules and coturn. It starts
isolated processes; `GATEWAY_TEST_JANUS_LIB_DIR` can select an extracted library
tree. Test peers use local interfaces, falling back to loopback when enumeration
is prohibited. Relay tests use authenticated local coturn, require only relay
candidates and assert relay nomination; no external TURN service is used. CI
runs these tests in its dedicated Janus job. Test adapters access aioice's relay
policy and credential rotation because aiortc lacks the corresponding public
browser APIs. Real-browser routing, durable presence and host-removal acceptance
remain later task gates.

Rollback: unset `JRTC_SIGNALLING_MEDIA_REHEARSAL` and drain the rehearsal owner.
No persistent gateway schema, existing meeting route or legacy media path changes.
Never switch an active gateway cohort back to legacy before confirmed cleanup.

Primary API references: [Janus VideoRoom](https://janus.conf.meetecho.com/docs/videoroom.html),
[SDP RFC 8866](https://www.rfc-editor.org/rfc/rfc8866.html),
[aiortc test peer API](https://aiortc.readthedocs.io/en/latest/api.html).
