# SG-11 — Django projection and frontend state notifications

**Repository scope:** synq  
**Depends on:** SG-10  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Project accepted events into Django durable permission-aware state and Redis transient online/media presence, with session generation and monotonic meeting version. Emit SSE after commit to authorised participants/moderators, supporting replay/event ID and fresh snapshot when SSE gap, safe disconnect, right-sized payloads. Reuse existing emitter if warranted; choose SSE without duplicating conflicting Socket.IO state.

## Required deliverables and verification
SSE routes + snapshot endpoint, auth/visibility tests, late old-session event tests, lost fanout recovery, no 1:1 high-frequency DB writes.

## Acceptance criteria
State versions never regress; waiting participant sees only permitted state; approval != connected; UI notified only after committed projection.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-11.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
