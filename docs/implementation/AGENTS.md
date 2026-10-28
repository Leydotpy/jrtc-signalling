# AGENTS.md — Dedicated Synq WebRTC Signaling Gateway

## Mission
Build and stage the dedicated **Python ASGI signaling server** first. Extract ownership of the live JRTC runtime/Janus sessions and browser media signaling from the Django ASGI process. Maintain Django as authority for meeting admission, membership, roles, removals, and long-lived state. Introduce secure Django-issued short-lived tickets, a Broka command/event boundary, a Django event projector/SSE path, and compatible `synq.js` client routing.

## Binding priorities
1. Never overwrite or silently replace existing repository `AGENTS.md` instructions. Read them and apply this pack as an additive task plan. Conflict? Report and block before destructive edits.
2. Inspect actual code/branches/worktrees, record SHAs and baseline tests before editing; do not assume these paths still exist. Use smallest incremental, reversible diffs. Never reset/rebase/clean/stash/discard unrelated work.
3. The design must be a **separate deployable service**, even if located in a local sibling folder initially. No Django ORM inside gateway. It uses internal RPC/events for coordination. JRTC remains framework-independent.
4. Permission decisions are Django's; the gateway validates current grants and enforces them. Gateway observations (connected, media publishing) may never grant admission or restore revoked access. **Fail closed** for admission/publish if authority or revocation state is uncertain.
5. SDP/ICE remain browser ↔ gateway ↔ JRTC ↔ Janus, **not through Django/Broka**. Browser ↔ Janus transports media; neither Django nor gateway relays media.
6. JRTC live sessions/handles are **owned by one gateway worker/generation**. Introduce owner routing + fencing before cutover; no concurrent active Django/Celery ownership of same resources. Preserve rolling upgrade safety.
7. Broka **commands** Django→gateway and **events** gateway→Django use durable, authenticated, bounded, idempotent, observable delivery with typed versions. Never block Janus transport/response loop on Broka, storage, analytics or SSE.
8. Django persists authorization and durable state. Connection state is an observation/projection; `approved != connected`, `disconnected != removed`, stale-session events cannot roll back new-session state.
9. Gateway validates every ticket (`iss`, `aud`, signature, expiry, nbf, jti, session, participant, scopes) and checks current permission/generation for access-changing actions. Never log tickets/SDP/ICE. Redact any sensitive event data.
10. Preserve explicit internal Janus `int` IDs and browser-facing decimal-string Janus IDs; ICE final batches precede completion, empty non-completion batch is invalid, no duplicate JSEP processing.
11. Reuse current protocol contracts during a *feature-flagged dual-path period*. Do not remove legacy namespace/runtime until all compatibility and rollback gates pass. No production deploy/release without user approval.
12. Do not modify unrelated P0 UI, sound cues, media filters, multi-language migration, recording, 90nutmegs, or platform branding.

## Execution mode
Default pack prompt = autonomous, **sequential**, one task at a time. Each successful task: implement + tests + cross-repo compatibility checks + per-repo commits + task report + tracker update, then next dependency-ready task. Stop on missing permissions, destructive conflict, failed checks, unresolved architecture ambiguity with security impact, or safety-critical failures. Review-gated prompt is provided as an alternate.

## Authoritative files
`EXECUTION_ORDER.md`, `contracts/*.md`, `architecture/BOUNDARIES.md`, `agents/*.md`, `tasks/SG-XX_*.md`, `tracking/progress.json`, `checklists/ACCEPTANCE.md`, `prompts/*.txt`.

## Definition of done
A local end-to-end test proves signed join/lobby/host admission → gateway JRTC/Janus signaling → gateway event → Broka → Django projected presence → frontend notification; host revocation disconnects media and prevents reconnect; failures/retries/restart/duplicate events are safe; old path remains rollback-ready. Production migration remains a separately approved action.
