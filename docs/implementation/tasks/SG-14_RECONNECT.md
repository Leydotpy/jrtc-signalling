# SG-14 — Reconnect, resumable sessions, stale event protection

**Repository scope:** gateway + synq.js + synq  
**Depends on:** SG-09, SG-12, SG-13  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Reconnect protocol: refresh ticket via Django when necessary; re-auth active epoch and admission; increment connection generation and fence old sockets; reclaim or recreate Janus resources only when safe; resubscribe/re-ICE and reconcile SSE snapshot; avoid repeat waiting-room approval when still approved. Handle token expiry midcall, browser tab switch, Janus session loss.

## Required deliverables and verification
Fault injection tests for 2 sessions same identity, delayed disconnect, expired/revoked grants, Janus loss, reconnect storm and browser offline.

## Acceptance criteria
Connected state not downgraded by stale event, revoked cannot resume, transport retry bounded, recovery UX consistent.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-14.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
