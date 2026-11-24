# Final acceptance checklist

These checks apply to the isolated SG-18 revisions. The report links each result to the runner, test or source that establishes it. [Test matrix and source revisions](../tracking/reports/SG-18.md) · [Clean installation and demo](../../ACCEPTANCE_DEMO.md)

## Implementation and compatibility

- [x] Match the gateway, Synq, browser client, JRTC and VideoRoom revisions. Verify the commit history for each recorded task and preserve unrelated work.
- [x] Build and start the standalone ASGI image. Verify the gateway source and runtime lock are independent of Django.
- [x] Verify signature, issuer, audience, key ID, expiry, single-use ticket consumption, binding, epoch and scope against Django. Confirm the lobby has no media rights.
- [x] Admit and deny through Django. Check permission changes, removal and meeting end against current membership and the committed decision.
- [x] Verify signed Broka commands, durable outbox/inbox, retries, deduplication, bounded retention and quarantine. Verify that command and event schemas exclude SDP, ICE and credentials.
- [x] Fence each active Janus resource to a single worker and generation. Confirm that legacy ASGI and Celery refuse management of gateway-assigned sessions.
- [x] Correlate publisher and subscriber JSEP, enforce operation and queue bounds, and suppress duplicate responses. Check final ICE batches, completion and restarts.
- [x] Receive real WebRTC media through Janus and verify TURN-only RTP using Chromium statistics.
- [x] Project signed, ordered observations into Django. Reject stale sessions, epochs and replayed events.
- [x] Read authorized state after commit via SSE, resume from a cursor and repair gaps with a quiet snapshot.
- [x] Select the gateway SDK by Django's session route. Confirm the staging browser opens only the gateway WebSocket and a separate legacy session still carries media.
- [x] Confirm physical disconnect on removal or end while broker delivery is paused. Require signed confirmation after recovery. Denied and removed profiles receive a refusal from fresh browser contexts.
- [x] Recover after disconnection, ticket expiry, Janus restart and worker death. Rehearse SIGTERM, a fenced handoff and rollback to a fresh session.
- [x] Check application logging with fixture requests and inspect final workflow job logs for meeting identifiers, credentials and raw media-signalling payloads.
- [x] Record unit, integration, browser, security, failure and bounded-load results. Reconcile all 19 reports with the tracker.

## Manual security and release review

| Review item | Isolated result | Check in the target environment before production |
| --- | --- | --- |
| Browser identity and tickets | Real Django sessions and short-lived signed tickets exercised for a host and two non-member profiles. Wrong scope, replay, stale identities and revoked access are covered by the suites. | Sign in through the deployed identity provider. Check exact allowed origins, key distribution/rotation, TLS termination, cookie settings and clock synchronization. |
| Authorization and private state | A denied guest and a removed attendee could obtain neither a fresh ticket nor the terminal state. A legitimately joined host could observe meeting end. | Review the actual room policies and moderator membership. Session-level bans apply to the current meeting session. |
| Worker, broker and Janus credentials | Signed publisher and owner identities, private VideoRoom access and failure on uncertain ownership were exercised. | Check the deployed broker ACLs, direct-worker TLS identities, Janus isolation and per-worker persistent inboxes. |
| Logs and metrics | Django request handling, Uvicorn access formatting, LogVista domain errors and gateway private telemetry passed the redaction checks. | Inspect a fixture join, denial and removal in the actual proxy, identity-provider, database and log-export sinks. Restrict metrics and verify retention. |
| Cutover and capacity | The staged canary, ownership drain, failback and two benchmark orders passed. | Measure the target meeting sizes, network conditions and resource budgets. Confirm a staffed rollback window and a separate release decision. |

- [ ] Production traffic switch approved by the maintainer. This is an external release decision.
- [ ] Legacy retirement approved after the fallback window and live-usage review. This is a separate change.
