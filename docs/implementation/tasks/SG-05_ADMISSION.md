# SG-05 — Django admission/waiting-room decisions

**Repository scope:** synq  
**Depends on:** SG-03  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Ensure waiting-room join intent created/validated through Django and host/mod permissions checked. Host API accepts/denies using server-side identity. Publish durable permission state changes using outbox strategy (actual delivery wired SG-06). Direct-entry meeting Django authorizes independently; gateway lobby arrival is observation only. Handle duplicate approval and revoke race by auth epoch.

## Required deliverables and verification
Idempotent admit/deny endpoints and regression tests, documented waiting room projections; no reliance on gateway to grant access.

## Acceptance criteria
Host and moderator checks enforced; denied users cannot enter; approved remains distinct from media-connected; commands based on committed decision.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-05.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
