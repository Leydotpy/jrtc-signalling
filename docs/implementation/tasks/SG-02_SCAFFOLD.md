# SG-02 — Create separately deployable Python ASGI gateway

**Repository scope:** gateway  
**Depends on:** SG-01  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Create minimal runnable `synq-signaling-gateway` Python package in dedicated existing repo or local sibling. ASGI application with lifespan, structured configuration, health/live/ready endpoints, configurable WebSocket route, dependency setup, dev Dockerfile/Compose override, unit tests and CI commands. No Django ORM or models imported. Support graceful shutdown and readiness for JRTC/Broka without prematurely connecting production services.

## Required deliverables and verification
Service tree, environment example (no secrets), run/test/lint commands, service ownership README, basic smoke test and CI.

## Acceptance criteria
Process starts standalone, starts/stops cleanly, bad config fails safely, health probes distinguish live/ready; zero dependency on Django project imports.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-02.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
