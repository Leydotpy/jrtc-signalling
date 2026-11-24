# SG-12 — Client transport adapter and Django SSE integration

**Repository scope:** synq.js  
**Depends on:** SG-08, SG-09, SG-11  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Inventory SDK Socket.IO abstractions. Add feature-flagged gateway WebSocket transport adapter, Django HTTPS ticket retrieval/refresh, SSE subscriber and state snapshot reconciliation. Existing React bindings and legacy Socket.IO remain operational. Avoid double-processing JSEP and unintentional double ownership; maintain sound event metadata. No UI layout or theme changes.

## Required deliverables and verification
Unit/SDK/react integration tests: direct join, waiting room, ICE batching, media publishing, SSE drop + resume, legacy transport.

## Acceptance criteria
Client can use either transport with same public SDK semantics; switching never signals simultaneously on both; id versions work.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-12.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
