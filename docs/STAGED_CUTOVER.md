# Staged meeting cutover and failback

Use this procedure on an isolated local or staging stack. Django owns the decision
for each new session. The room UUID selects future sessions. The session UUID
identifies an immutable transport assignment and is the ID used by the gateway
and browser. Participants need a new meeting link after failback.

## Dark launch

Apply Synq migration `0021_gateway_room_canary`. Leave room selections empty.
Start Django, its ordinary legacy workers, the signed gateway command worker
(`run_gateway_commands`), and the lifecycle projector (`run_gateway_events`).
Keep Django, Janus, Redis and their credentials isolated from production.

| Component | Settings on the staging stack |
| --- | --- |
| Django | Set `GATEWAY_STAGED_ROUTING=true`. Enable `GATEWAY_TICKETS_ENABLED`, `GATEWAY_ADMISSION_ENABLED`, `GATEWAY_COMMANDS_ENABLED`, `GATEWAY_RUNTIME_ENABLED`, `GATEWAY_EVENTS_ENABLED` and `GATEWAY_STATE_ENABLED`. Supply the existing issuer, signing keys, authority token and bus config. |
| Web app | Set the server environment variable `SYNQ_STAGED_ROUTING=true`. Build with `NEXT_PUBLIC_SYNQ_GATEWAY_URL` set to the staging WSS ingress. Use HTTPS for the authenticated Django backend. |
| Gateway | Prepare `auth.json`, `bus.json`, `runtime.json` and their referenced key files. The staged Compose overlay mounts them read-only under `/run/gateway`. Set `inbox_path` in the gateway bus config to a file under `/var/lib/gateway`, on a separate durable volume for each worker. |
| Ingress | Terminate public WSS at the staging TLS proxy. Forward to the loopback-bound gateway listener and admit traffic only while `/health/ready` returns 200. The worker checks the same conditions on new WebSockets. |

Create an unselected room and start a session. An authenticated HTTPS GET of
`/api/v1/meetings/sessions/SESSION_UUID/signalling-route/` returns
`{"session_id":"…","backend":"legacy"}`. Publish from that meeting and keep it
running through the canary. Existing live meetings keep their assigned backend
when an operator changes a room selection. Room creation requests cannot set
the operator's selection field.

## Admit one isolated canary

1. Create a separate room and record its UUID. In the Synq checkout run
   `python src/manage.py gateway_cutover select ROOM_UUID`. Start a new session
   through the normal room start endpoint and record the returned session UUID.
2. Give the isolated worker this session UUID in the `meetings` array of its
   runtime config. Use one worker dedicated to the canary for this rehearsal.
   The meeting page can return unavailable until the worker acquires ownership.
   Keep the session link internal during bootstrap.
3. Set `GATEWAY_STAGE_CONFIG_DIR` to the prepared configuration directory on
   the gateway host. Start the worker and wait on its health check.

   ```sh
   docker compose -f compose.yaml -f compose.staged.yaml up -d --wait --wait-timeout 90 gateway
   ```

4. Confirm the readiness response reports all four dependencies ready. Check
   that the route for the new session is `gateway` and that the dark meeting
   still returns `legacy`. Join with a host and an attendee. Confirm audio and
   the authenticated state stream. The browser routes directly to the gateway
   for signalling. An unavailable or mismatched route stops the join.
5. Compare the SG-16 ICE acknowledgement distribution and error counters with
   the same staging baseline. Check outstanding commands, projector lag, and
   presence replay. Pause the cohort on failed readiness, unexpected media
   ownership, growing command lag, or a replay discrepancy.

For the multiworker rehearsal, retain the SG-15 pool config and direct worker
TLS endpoints. A standby without a live lease has no public readiness. During
failback, retire the isolated canary pool only after the termination outcome
is durable. Rolling handoff of a live meeting is covered in the ownership guide.
[Runtime configuration and ownership](RUNTIME_OWNERSHIP.md)

## Pause and roll back

1. Run `python src/manage.py gateway_cutover pause ROOM_UUID` to stop new
   selections. The active gateway session can continue using its existing link.
2. To end that exact session, run
   `python src/manage.py gateway_cutover drain ROOM_UUID --session SESSION_UUID`.
   This also pauses the room. Keep the gateway and its command/event workers
   running. The old link now returns 410. The terminal SSE snapshot shows the
   ended meeting and offline participants.
3. Inspect durable outcomes in Synq. Export the old UUID as
   `SG17_SESSION_ID` and run the following read-only command. Wait until
   `meeting.terminate` is applied and the counts for pending and accepted
   are zero. Review any other failed or superseded command against the final
   moderation state.

   ```sh
   python src/manage.py shell -c '
   import os
   from django.db.models import Count
   from apps.meetings.models import GatewayAuthorizationOutbox, GatewayCommandRoute
   session = os.environ["SG17_SESSION_ID"]
   print(list(GatewayAuthorizationOutbox.objects.filter(session_id=session).values("action","status").annotate(total=Count("pk"))))
   print(list(GatewayCommandRoute.objects.filter(session_id=session).values("owner_generation","released")))
   '
   ```

4. Stop the dedicated gateway gracefully, then repeat the read-only check and
   confirm `released=True` for the old owner. Stop the isolated worker with
   `docker compose -f compose.yaml -f compose.staged.yaml stop gateway`. Keep
   its persistent inbox and the Django audit records. An unclaimed cold canary
   can be closed immediately after Django settles its unsent commands.
5. Start the room again through the normal endpoint. Django checks the
   terminal outbox, the signed VideoRoom termination outcome for the exact
   owner generation, and the clean owner release inside the room creation
   transaction. Record the **new** session UUID, check its legacy route, and
   distribute its link to staging participants. Confirm audio on both legacy
   sessions and replay the old session's state stream from its last cursor.

Lease expiry, a stopped process, or a timed-out termination leaves the room
blocked for investigation. Preserve the outbox and owner records, restore the
isolated dependencies, and check the exact Janus session and room. Clearing
leases or overriding the guard would lose the evidence required for failback.

## Verification and release decision

The Synq control-plane workflow runs the reproducible rehearsal with local
PostgreSQL, Redis, Janus and real WebRTC audio peers. From the Synq checkout
with those test services available, run the following command.

```sh
DJANGO_SETTINGS_MODULE=conf.gateway_postgres_test_settings \
uv run --frozen --with pytest --with aiortc python scripts/check_gateway_cutover.py --gateway ../jrtc-signalling
```

Supply `GATEWAY_TEST_DB_PASSWORD` and an isolated `GATEWAY_TEST_REDIS_URL`.
The check uses a disposable Django test database. It verifies that the old
Janus session returns error 458 before opening the legacy replacement. The
browser workflow exercises route discovery with Chromium, TURN and the React SDK.

Production requires a separate maintainer go/no-go after SG-18. That decision
includes the target cohort, observed load and media quality, capacity and
rollback staffing. This procedure authorizes isolated staging only.
