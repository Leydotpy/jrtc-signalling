# SG-06 — Reliable command plane and transactional outbox

**Repository scope:** synq + gateway  
**Depends on:** SG-01, SG-05  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Choose supported durable Broka backend currently configured; implement Django transactionally stored outgoing `participant.admit|deny|disconnect|permissions.update|meeting.terminate` commands with request IDs/epoch/owner targeting. Background dispatcher with ack, retry/backoff, DLQ; gateway authenticated bounded consumer with dedupe/inbox, accepted/applied/failed outcomes. Broker outages do not authorize unintended access.

## Required deliverables and verification
Outbox/inbox models or reusable infrastructure, integration tests for publish failure, duplicate command, slow consumer, TTL, no owner, conflict and restart.

## Acceptance criteria
Committed Django control action survives broker outage; same command applied at most once semantically; failed enforcement reported; gateway Janus receive never blocks on Broka.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-06.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
