# SG-18 — End-to-end evidence and readiness report

**Repository scope:** all  
**Depends on:** SG-17  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Verify full path (user/guest auth→lobby→host admit→WebSocket→JRTC Janus publish/subscribe→gateway lifecycle Broka→Django projection SSE→React). Exercise removal, bans, reconnect, multiworker, failure, rollback. Run static checks/test suites and compatibility audit; resolve or enumerate unresolved risks. Verify all per-task reports and SHAs/tracker.

## Required deliverables and verification
Final test matrix, known limitations, manual security checklist, build/run instructions, release readiness with production cutover explicitly excluded.

## Acceptance criteria
Documented clean install and local reproducible demo, passing mandatory checks and unambiguous completed vs pending vs blocked tasks; no premature production switch.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-18.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
