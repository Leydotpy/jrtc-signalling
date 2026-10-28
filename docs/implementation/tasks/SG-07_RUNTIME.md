# SG-07 — Extract active JRTC session/handle ownership

**Repository scope:** gateway + synq (JRTC only if needed)  
**Depends on:** SG-02, SG-06  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Adapt known `JanusProcessRuntime`, `JrtcHandleRegistry`, and `VideoRoomAdapter` boundaries from Synq into gateway-friendly interfaces without importing Django. Instantiate generic `JanusSessionManager` and `jrtc-video` under gateway ASGI lifespan. Route operations using owner ID/generation; represent opaque handles in gateway, never ORM plugin instances. Preserve generic event publisher and loss observer; initial non-prod standalone Janus connection. Prevent active Django+gateway ownership for same session (explicit cohort fence).

## Required deliverables and verification
Gateway runtime adapter, health/lifecycle tests, ownership tests, clean failure/restart/handle disposal, manifest of deferred Django paths.

## Acceptance criteria
One live owner per active handle; no ORM in gateway; external worker can identify owner and send command; Janus ids typed correctly; no duplicate handle activation.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-07.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
