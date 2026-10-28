# SG-03 — Django ticket issuance and permissions epoch

**Repository scope:** synq  
**Depends on:** SG-01  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Add authenticated HTTPS endpoint/service for scoped short-lived gateway ticket issuance for existing user and allowed guest. Reuse current meeting/participant models; include audience/issuer/key-id/jti/exp/nbf/session and epoch scopes. Define ticket refresh, room status, bans, lobby-only behavior, rate limits and correlation IDs. Atomic state changes bump permissions epoch. No Janus changes.

## Required deliverables and verification
Migration only if justified, tests for registered/guest/lobby/banned/ended/expired/wrong roles, documented key rotation and session binding.

## Acceptance criteria
Ticket has least privilege; unauthorised guests/banned/ended cannot mint playable media scopes; never leak signing keys/tokens.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-03.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
