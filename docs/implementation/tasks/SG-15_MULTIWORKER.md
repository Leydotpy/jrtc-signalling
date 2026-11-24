# SG-15 — Gateway owner routing, leases and graceful drain

**Repository scope:** gateway + synq  
**Depends on:** SG-07, SG-10, SG-14  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Implement Redis/broker-backed meeting->gateway ownership lease/routing and fencing generation, renewal/expiry and single-flight execution. Include sticky WebSocket route or verified reroute to owner; commands address correct owner. On lost ownership stop commands, detach/cancel and reconcile. Rolling restart drain. Avoid split brain and duplicated Janus room operations.

## Required deliverables and verification
Two-worker integration stress tests, forced lease expiry, concurrent owner election, stale ack, Redis outage, command replay, graceful SIGTERM.

## Acceptance criteria
One owner per session; stale worker cannot mutate Janus after fencing; recovery/rollback demonstrable without duplicate presence.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-15.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
