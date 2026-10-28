# SG-00 — Cross-repository baseline, branch/ownership audit

**Repository scope:** all repos  
**Depends on:** none  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Record `git status --porcelain`, remotes, current SHAs, existing AGENTS.md, package versions, active Janus connection creators, Socket.IO event/SDK interface and deployments. Locate gateway repo if present; record new local sibling folder decision if absent. Inspect `src/conf/asgi.py`, `src/apps/meetings/realtime/namespace.py`, `src/apps/meetings/services/signaling.py`, `services/janus.py`, `jrtc/runtime.py`, `handles.py`, Celery boot/lifecycle, Broka consumer paths, frontend calls, JRTC/VideoRoom primitives. Determine backend test commands without altering worktree. Map owners/competing dual runtimes and feature flags.

## Required deliverables and verification
No code modifications unless a harmless baseline test fixture is essential. Evidence file paths/line refs; test results; risk/dependency ledger and exact existing/unknown APIs.

## Acceptance criteria
Baseline tests run (or documented failure) and baseline report includes per-repo branch+SHA+dirty status, topology diagram, integration seams and build target; no forced branch or destructive cleanup.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-00.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
