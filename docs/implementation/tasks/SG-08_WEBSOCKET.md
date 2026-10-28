# SG-08 — Browser⇄gateway signaling operations

**Repository scope:** gateway  
**Depends on:** SG-04, SG-07  
**Status source:** `tracking/progress.json`

## Objective
Deliver one reviewable component of the dedicated gateway, strictly within the architecture/contract boundaries and without unrelated refactors.

## Required implementation
Implement validated request/response correlation in async WebSocket layer, bounded concurrency and queue, publisher/subscriber join/configure/publish/unpublish/update/start/detach, SDP offer/answer/JSEP routing and independent Janus unsolicited event forwarding. Wire directly to `jrtc-video` async methods; authorization cached validated on sensitive operations; no intermediary Django ORM/Broka per negotiation.

## Required deliverables and verification
Protocol tests, end-to-end mock Janus interaction and correct error close codes, timing metrics, cancellation tests.

## Acceptance criteria
Publisher+subscriber operations complete without django/broker hop in hot path; responses correlate correctly; unauthorized media actions fail; no full SDP logging.

## Procedure and reporting
1. Read pack `AGENTS.md`, the corresponding repo overlay in `agents/`, and all prerequisites' reports. Examine existing repository `AGENTS.md` and code; identify touched files and record current SHA/worktree status.
2. Implement minimal changes behind compatibility/feature flags where relevant. Write positive, negative, concurrency/failure tests before asserting completion.
3. Run applicable lint/typecheck/tests and targeted integration suite. Capture exact commands/results; do not claim pass unless executed. Security-critical test failure or inconsistent contract => BLOCKED.
4. Commit implementation **in each modified repo** to current safe feature branch (never commit to protected branch or overwrite unrelated dirty files). Record repository/branch/commit SHA; record deliberate no-code repo impacts.
5. Create `tracking/reports/SG-08.md` using `tracking/REPORT_TEMPLATE.md` with code file references, behavior, commands, evidence, outstanding risks; update `tracking/progress.json` using `scripts/tracker.py` and `PROGRESS.md` (update tracker in pack working tree; do not falsely claim cross-repo commit of the pack itself).
6. In autonomous mode continue only if all dependencies complete, tests pass, code is committed, and there is no blocker. In review-gated mode stop at READY_FOR_REVIEW.
