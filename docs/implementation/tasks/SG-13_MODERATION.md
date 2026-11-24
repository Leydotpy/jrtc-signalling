# SG-13 — Revocation, denial, kick, and meeting termination

**Repository scope:** synq + gateway  
**Depends on:** SG-06, SG-08, SG-11  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Implement Django-authoritative host/mod control, monotonic epoch revocation, direct gateway current-grant enforcement, JRTC detach/stop/close, success/failure events, pending enforcement status, retry/reconcile. Guest denial, banning and end-meeting prevent stale token republish/resume. Rate-limit and audit moderation.

## Required deliverables and verification
Tests of kick during publish/ICE, host role change, broker down, revoke before admit, gateway down, duplicate command, false success.

## Acceptance criteria
Revoked user cannot publish/subcribe/rejoin; physical disconnection eventually reconciles; no success reported solely on queue.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-13.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
