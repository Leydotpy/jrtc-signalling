# SG-04 — Gateway WS verifier and permission enforcement

**Repository scope:** gateway  
**Depends on:** SG-02, SG-03  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Validate Django ticket cryptographically, origin/audience/claims/expiry/session/generation, initial scopes and active grant/epoch via trusted service lookup/cache. Implement WebSocket handshake, per-connection rate/memory/size limits, timeouts, policy-safe close codes and graceful ticket refresh. `lobby` cannot call media functions. If authority unavailable on privileged transition fail closed.

## Required deliverables and verification
WebSocket auth middleware/endpoint, negative auth tests, replay/revocation race tests, log redaction.

## Acceptance criteria
No unauthenticated or expired session can signal; forged session/meeting and cross-origin failures reject; auth does not query Django for every ICE candidate.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-04.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
