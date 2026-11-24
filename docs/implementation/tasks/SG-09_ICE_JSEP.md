# SG-09 — Batched ICE, completion, and JSEP ordering

**Repository scope:** gateway + synq.js fixtures  
**Depends on:** SG-08  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Consume existing `normalize_ice_request` semantics and client batcher behaviors, preserve array candidate batches, legacy singular candidate compatibility (temporary), final batch before completion, per-handle queue ordering, proper empty completed flag, publisher+subscriber trickle and ICE restart. Deduplicate Janus correlated vs unsolicited JSEP using correlation/generation; process unsolicited JSEP. Track stable string Janus IDs at wire boundary.

## Required deliverables and verification
Matrix: single/16/final-3 empty-complete malformed oversized out-of-order reconnect TURN-only; JSEP duplicate/unsolicited tests.

## Acceptance criteria
No final candidate loss; no false completion; one batch one JRTC batch operation where supported; no ICE on stale owner.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-09.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
