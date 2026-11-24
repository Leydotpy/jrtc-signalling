# SG-17 — Feature-flagged local/staged cutover & rollback

**Repository scope:** synq + synq.js + gateway  
**Depends on:** SG-13, SG-15, SG-16  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Implement routing configuration for selected new meeting cohorts; disable active JRTC ownership in Django ASGI/Celery **only for gateway-owned cohorts**, preserving legacy for other cohorts; ensure no mixed ownership across same meeting. Health-gated deployment config, dark launch, local canary, failback that drains old gateway sessions before legacy re-create. Keep rollback switches and prohibit production rollout/removal of old path without user approval.

## Required deliverables and verification
Feature flag config, runbooks, no-double-owner tests, rollback rehearsal and verify pending commands and SSE consistency.

## Acceptance criteria
Local or staging canary passes; rollback tested without active dual attachments; separate go/no-go for production documented.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-17.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
