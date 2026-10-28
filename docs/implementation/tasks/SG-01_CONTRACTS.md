# SG-01 — Versioned signaling/command/event compatibility contracts

**Repository scope:** all repos  
**Depends on:** SG-00  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Using real code, finalize the draft operation names in `contracts/PROTOCOL.md`, Django commands and gateway lifecycle events from `contracts/EVENTS.md`, ticket fields/security from `contracts/SECURITY.md`. Build old Socket.IO→gateway op mapping, Janus/JSEP correlated/async event map, per-session ordering/epoch/version rules, failure semantics and reason codes. Choose SSE state delta/snapshot reconciliation API without unnecessary new parallel frontend state model.

## Required deliverables and verification
Versioned JSON Schema or Pydantic/TypeScript models as appropriate; contract fixtures and schema compatibility tests. ADR identifying deployment layout, delivery guarantees and fallback route.

## Acceptance criteria
Schema examples validate; no unreviewed breaking change to existing `synq.js` calls; full contract matrix for auth, waiting room, media, moderation, recovery.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-01.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
