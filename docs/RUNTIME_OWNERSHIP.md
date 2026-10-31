# Gateway runtime ownership (SG-07)

The optional `runtime` extra pins the same reviewed JRTC 3.2.0 and jrtc-video
3.0.3 commits as Synq. It adds an ASGI-owned, single-session manager and an
in-memory handle registry. No Django module, persisted plugin object or saved
Janus ID is used to acquire a live handle. Core JRTC alone replaces lost sessions.

This is an isolated rehearsal component. Browser media operations remain SG-08;
moderation enforcement remains SG-13. Overall readiness stays 503 even when
Janus, broker and ownership probes are healthy. A correctly routed command still
reports failed/unavailable until its physical operation is implemented.

## Configuration

Install with `uv sync --locked --group dev --extra runtime`. Configure the signed
[command plane](COMMAND_PLANE.md) first. Add `JRTC_SIGNALLING_RUNTIME_CONFIG_FILE`
pointing to a protected JSON file:

```json
{
  "janus_url": "wss://isolated-janus.example/",
  "api_secret_file": "/run/secrets/isolated-janus-api-secret",
  "ownership_url": "https://synq.example/api/v1/meetings/internal/gateway/owners/",
  "meetings": ["00000000-0000-4000-8000-000000000001"]
}
```

The secret must contain at least 32 characters. Plain WebSocket is permitted
only on loopback. The ownership endpoint requires HTTPS, the service bearer
credential from bus configuration, and a request signed with the gateway's
trusted Ed25519 bus identity. Redirects, URL credentials and query strings are
rejected. Use an isolated non-production Janus server; no endpoint is selected
automatically from Django's legacy configuration. Keep Janus session reclaim
disabled; gateway recovery always creates fresh sessions/handles.

In Synq, enable `GATEWAY_RUNTIME_ENABLED` along with the command/admission flags,
and set both `gateway_control_plane` and `gateway_runtime` on the rehearsal room
before creating its meeting session. The new session persists
`signalling_backend=gateway`; existing sessions remain legacy. This field cannot
be changed through model saves or queryset updates. Turning flags off never
makes an assigned meeting eligible for legacy Janus work. Start a new session
for any later cutover or rollback, after draining the old owner.

SG-07 accepts at most 16 explicitly configured meetings and 256 local handles.
Keep initial rehearsals small. Set the gateway shutdown budget to 30 seconds to
allow bounded handle/service/session cleanup and ownership release; larger
cohorts may also require a larger startup budget. No routing/load-balancing
service or production deployment is created here.

## Ownership and recovery

Django serializes claims under the meeting row lock. A lease binds the trusted
owner ID, a fresh process-incarnation UUID and a monotonically increasing
generation. The route is published in GatewayCommandRoute for the SG-06
dispatcher. Claims, renewals and releases carry a UUID request ID and a bounded
timestamp. Retired incarnations are remembered so an old signed claim cannot
resurrect a released owner. A retry of a current claim returns its original lease.

Leases last 30 seconds and renew every five seconds. The gateway calculates its
deadline from before the RPC and reserves one second, so network delay cannot
extend ownership. Every registry/management operation checks current owner,
generation, loop, lease and actual session object, then runs within the smaller
of its three-second budget and remaining lease. Cached handles do not bypass
these checks. An unknown result invalidates the handle token; it is not replayed.

Opaque handle tokens bind meeting, participant/session, role, owner generation
and actual Janus session/handle identity. Concurrent attachment of the same key
returns one binding. Detach and invoke share a lock. Internal Janus identifiers
remain positive `int`; the wire conversion returns decimal strings. IDs supplied
by another process or recovered from a database can never attach a plugin.

The manager's loss handler is never replaced. Separate constant-time observers
wake one coalescing reconciliation task. Old bindings/services become unusable;
the manager creates a fresh session, and later explicit operations use a fresh
VideoRoomService and fresh handles. No old opaque token becomes valid again.

If lease renewal fails, the runtime stops operations and disposes its resources.
Clean shutdown closes handles and services before sessions, then drains the
generic publisher and releases the exact owner generation. A failed cleanup
does not publish a successful release. Expired ownership is not proof of remote
cleanup: SG-07 refuses automatic takeover. Reconciled crash takeover, cross-worker
routing and rolling drains belong to SG-15. Never clear a route manually merely
to bypass a failed cleanup; first reconcile the isolated Janus resources.

The generic event publisher is retained with a bounded in-process diagnostic
sink. Only fixed-field `webrtcup`, `detached` and `timeout` observations enter it.
Plugin payloads, SDP/JSEP, ICE, reason text and arbitrary extras are excluded.
This sink is not a durable application event plane; SG-10 adds that projection
boundary. No storage or broker await is added to the Janus receive callback.

## Legacy path manifest

| Synq path | SG-07 treatment |
| --- | --- |
| `conf/asgi.py`, Celery runtime hooks | Retained to serve legacy cohorts; no global shutdown/cutover |
| `services/lifecycle.py:start_session` | Assigns new cohort; gateway host is admitted without media presence or legacy provisioning |
| `services/janus.py` management/attach facade | Rejects gateway sessions before legacy Janus work |
| `services/signaling.py` claim/prepare/ICE | Rejects gateway cohort, including the cached ICE path |
| `tasks.py` provision/prepare/sync/destroy/detach | Skips gateway resources; durable commands retain the control boundary |
| Browser Socket.IO signalling | Kept for legacy cohorts; gateway media WS dispatch is SG-08/12 |
| Presence projection and moderation | SG-10/11/13; no physical enforcement is inferred from a lease |

## Verification

Local unit and database tests cover signature and cohort checks, competing claims,
retired-incarnation replay, expiry, stale/forged handles, concurrent attachment,
session replacement and cleanup/cancellation failures. PostgreSQL establishes
cross-worker locking. CI's isolated Janus job installs Ubuntu's Janus package,
starts only loopback WebSocket and VideoRoom modules with a temporary API secret,
and exercises actual session creation, reusable management, concurrent attachment,
disposal and server-loss/replacement. It creates no media peer or deployment.

Opt in locally only with Janus and Redis available:

```sh
GATEWAY_TEST_JANUS=1 GATEWAY_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
  uv run --extra runtime pytest -q tests/test_janus_live.py
```

The SG-07 task report records executed results; a skipped live check is not a pass.
