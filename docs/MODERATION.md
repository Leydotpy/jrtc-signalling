# Moderation and physical enforcement (SG-13)

The opt-in media rehearsal now enforces Django's signed `participant.admit`,
`participant.permissions.update`, `participant.disconnect`, `participant.deny`
and `meeting.terminate` commands. Django remains the permission authority. The
existing command consumer verifies the signature, destination, deadline, durable
command identity and current Django authorization before invoking the live owner.
An admission command never creates a media connection or grants permission by
itself: the client must present a current Django-issued ticket.

## Revocation barriers

Before awaiting any I/O, enforcement installs a monotonic participant epoch
barrier and invalidates older sessions. Denial and disconnect also reject the
command's own epoch. A later allow command at that same epoch cannot undo the
denial. Meeting termination permanently closes that meeting ID in this worker.
A genuinely newer authority-validated participant epoch can be admitted after a
participant-level revocation; it cannot reopen a terminated meeting.

Hello/resume and refresh check the barrier after their authority RPC. In-flight
authorization replies cannot restore a revoked session. Revocation cancels the
socket's queued and running operations; the existing shielded cleanup joins those
tasks before disposing publisher and subscriber handles. A late SDP/ICE reply
cannot become a successful media acknowledgement. Other meetings remain active.

Every connection also rechecks current authority once per second, independently
of the command broker and client traffic. Checks serialize with that peer's media
operations and refresh, so a temporarily empty grant during an RPC is not treated
as permission removal. The guard has a six-second budget including lock wait;
HTTP authority requests have their own two-second default budget. Denied,
unavailable or expired authority closes the session. These are local bounds,
not a measured production revocation latency guarantee.

## Physical confirmation and retry

The gateway keeps the original Janus session/handle IDs until remote detach is
confirmed. SDK-local cleanup alone is insufficient. Confirmation requires a
successful detach or Janus error 458/459 (the original session/handle no longer
exists). Other errors, timeouts and lost transports retain an uncertain binding.
A replacement transport can confirm those immutable old IDs without adopting the
old plugin or making its opaque handle token usable again.

One reconciliation task retries up to eight uncertain bindings per pass, rotating
failures fairly. It is separate from lease renewal, command transport and the
Janus reader. Retried requests remain generation-fenced and bounded. New media
for the affected participant is denied while its old handle remains uncertain.
Uncertain cleanup degrades ownership health and prevents successful lease release.
An attach with no known returned handle ID closes admission for that meeting and
retains the owner fence for resource reconciliation; it cannot be reported applied.

Meeting termination closes its sessions, reconciles their handles, destroys the
private room and checks that the room is absent. Failed destruction retains the
room ID/secret for retry. A terminated meeting's existing lease is not extended;
Django's expected renewal refusal does not drain other meetings on the worker.
Cleanup and outcome delivery use the remaining finite lease. Expired ownership
does not authorize takeover or unbounded cleanup retries under a new owner.

## Outcomes, bounds and audit

`accepted` means only that the durable inbox reserved a command. `applied` is
written only after the required physical cleanup and final owner check complete.
The existing three-second command budget can produce `timed_out`; failures and
timeouts retain the revocation barrier and allow owned cleanup to continue.
Reconciliation does not rewrite a historical failure or timeout as success.
Duplicates replay their original durable outcomes without repeating enforcement.
Operators must reconcile failed/uncertain outcomes before submitting replacement
intent; queue publication is never evidence of disconnection.

The existing inbox/outcome records audit command ID, actor, scope, epoch, owner,
trace and result. Moderation also keeps fixed-label counters. A per-worker token
bucket allows a burst of 20 physical enforcement attempts and replenishes ten per
second. Rate limiting occurs after deny barriers and socket cancellation are
installed; it cannot preserve revoked media. It reports failed/unavailable while
background cleanup continues. Participant epoch barriers are capped at 10,000;
capacity exhaustion closes that meeting instead of evicting an older deny fence.
Existing connection, handle, packet, inbox and outbox limits still apply.

## Verification and boundaries

`tests/test_moderation.py` exercises the actual gateway/JRTC protocol with a
controllable Janus wire fixture: publish/ICE races, late hello/refresh responses,
same-epoch denial, stale admit, duplicate commands, permission changes, unavailable
authority, no owner, uncertain detach, timeout, rate limits and failed room cleanup.

`scripts/check_django_moderation.py --synq <checkout>` runs in Synq's Python 3.14
environment with this checkout, pytest and aiortc installed. It requires isolated
PostgreSQL/Redis (`conf.gateway_postgres_test_settings`, `GATEWAY_TEST_REDIS_URL`)
and a Janus binary. It verifies signed Django admission/removal/termination,
actual WebRTC/Janus disconnection, stale-ticket rejection, persisted command
outcomes, host membership-role revocation and a real Redis stall. The CI workflow
pins the compatible Synq revision and runs this test independently.

Production readiness remains 503 and the legacy path remains available. This task
does not certify automatic reconnect (SG-14), reconciled crash/failover takeover
(SG-15), deployment load/latency (SG-16), or production cutover. The authority
preflight on sensitive operations is retained; periodic checks add authority
traffic that must be included in later capacity measurements.
