# SG-16 — Performance, resilience and instrumentation

**Repository scope:** all applicable  
**Depends on:** SG-12, SG-15  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Add low-cardinality metrics for handshake success/failure, authorization errors, ICE p50/p95/p99, event-loop lag, Janus session/handles, broker DLQ/lag, permission reconciliation, SSE lag and reconnect. Trace meeting/session with pseudonymous IDs; log no tickets/JSEP/ICE strings. Load tests with multiple participant/media flows, broker and Janus faults.

## Required deliverables and verification
Load benchmark script/report, structured logging integration with existing logvista if applicable, alert thresholds, memory/queue caps and dashboards docs.

## Acceptance criteria
No unbounded queues; declared bottlenecks measured; baseline vs gateway results recorded, no fabricated improvements.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-16.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
