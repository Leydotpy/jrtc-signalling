# Synq Signaling Gateway — Standalone Codex Agent Pack
**Prepared:** 2026-10-27  | **Purpose:** extract and implement *only* the dedicated Python ASGI signaling gateway from the larger Synq/JRTC modernization programme.

## Scope
A separately runnable/deployable **Python ASGI WebSocket signaling gateway** using the existing `jrtc` and `jrtc-video` libraries. Django stays the authoritative identity/admission/meeting-control plane. Broka transports durable commands and lifecycle events (never SDP/ICE). `synq.js` uses Django HTTP/SSE for application state and the gateway WebSocket for live signaling. Browser media flows directly to Janus. The gateway is implemented and integration-tested before legacy ASGI ownership is retired.

## Entry points
1. Read `AGENTS.md` (pack-level entrypoint, **not** a replacement for any repository's existing AGENTS.md).
2. Read `architecture/BOUNDARIES.md`, `contracts/PROTOCOL.md`, `contracts/EVENTS.md`, `contracts/SECURITY.md`.
3. Start with `tasks/SG-00_BASELINE.md`, continue via `EXECUTION_ORDER.md` and `tracking/progress.json`.
4. Read repository-specific overlays in `agents/`. Report and commit each successful task, with evidence. Respect blockers and dependencies.
5. Use `prompts/KICKOFF_AUTONOMOUS.txt` to start an autonomous Codex run, or `prompts/KICKOFF_REVIEW_GATED.txt` for manual approval after each task.

## Repositories & refs (targets, not pinned SHAs)
- `Leydotpy/synq`: `v4` — Django/Socket.IO and current JRTC ownership
- `Leydotpy/synq.js`: `codex/batched-ice-meeting-sounds` — browser SDK/frontend
- `Leydotpy/jrtc`: `main` — generic async Janus runtime
- `Leydotpy/jrtc-plugins`: `main`, `jrtc-video` package — VideoRoom adapter
- **Standalone gateway home**: prefer an existing dedicated gateway repository if one exists in the supplied workspace; otherwise create a sibling **local** folder `synq-signaling-gateway/`, with its own package, config, health checks, Dockerfile and tests. Do **not** create a remote GitHub repo unless separately instructed. Record physical placement in SG-00 report.

This pack is an *extracted implementation plan*, not a completed gateway or a claim that the entire migration plan has already run. It can coexist with earlier P0 and multi-language packs. Avoid changing unrelated concerns.

## Workflow and audit
`NOT_STARTED → IN_PROGRESS → DONE` or `BLOCKED` (optionally `READY_FOR_REVIEW` in review mode). Agent writes `tracking/reports/SG-XX.md`, updates `tracking/progress.json` and `PROGRESS.md`, commits on the correct repository, records SHAs per repository and tests. Do not fabricate success, test results, or commits. Inability to commit or security/test failure is a blocker and halts autonomous execution.

The pack contains **instructions**, contract drafts, task files, checklist, and tracker tooling. It contains no claim that production code has been deployed or changed.
