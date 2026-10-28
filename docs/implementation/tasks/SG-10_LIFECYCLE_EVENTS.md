# SG-10 — Gateway lifecycle events and Django Broka consumer

**Repository scope:** gateway + synq  
**Depends on:** SG-06, SG-07  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Emit connection/lobby/media/disconnect/recovery events with event_id/session/generation/seq/epoch/tracing. Bounded asynchronous event publication; preserve JRTC raw `janus.events` pipeline and any `Dispio` logical dispatcher without duplicating action. Django authenticated consumer validates source & ordering, inbox dedupe; critical events replay/reconcile if broker unavailable.

## Required deliverables and verification
Event schema fixtures; tests duplicate, reordered, dropped, stale generation, poison message DLQ, producer restart, publisher backpressure.

## Acceptance criteria
Django consumes and records correct session observation; events never promote permissions; Janus transaction reader remains nonblocking.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-10.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
