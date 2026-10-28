# Final acceptance checklist

- [ ] Baseline SHAs/worktrees/AGENTS documented; no unrelated changes overwritten
- [ ] Standalone Python ASGI gateway deployable without Django import/ORM dependency
- [ ] Django issues short-lived lobby/media tickets; signatures/issuer/audience/jti/expiry/epoch verified
- [ ] Admission/deny/removal only Django-authorized; gateway reports enforcement outcome, not authorization decisions
- [ ] Broker durable authenticated commands/outbox/inbox/idempotency/retry/DLQ; no SDP or ICE through broker
- [ ] Single fenced owner for each active JRTC session/handle; no active dual ownership with Django ASGI/Celery
- [ ] Publisher/subscriber JSEP/WebSocket operations async, correctly correlated, bounded and test-covered
- [ ] ICE candidate batching complete/final order and restart; no duplicate correlated JSEP
- [ ] Janus media browser↔Janus, not gateway proxy; TURN fallback tested
- [ ] Events validated/idempotent/ordered into Django projection; stale session events ignored
- [ ] Django SSE after committed updates; auth filtering, replay or snapshot repair
- [ ] synq.js feature-flagged SDK adapter; existing meeting UI/legacy path preserved
- [ ] Revoke/ban/end-meeting kill access even with broker outage; reconciliation if enforcement delayed
- [ ] Reconnect and worker failure safe, no stale grants/handles; drain and rollback proven
- [ ] No leaking token, credential, SDP, ICE or private room info in logs
- [ ] Tests (unit/integration/e2e/security/failover/load) run with recorded evidence
- [ ] Per-task branch/commit reports exist, tracker reconciled, production cutover separately approved
