# Source notes and baseline constraints
This plan extracts the gateway architectural slice from earlier Synq/JRTC discussions and existing migration/performance AGENTS instructions. It *replaces the physical topology proposed in older async-first in-process signaling instructions*, not their ICE/JSEP/performance invariants.

## Relevant earlier source instructions inspected
- `AGENTS-synq.md` (`Synq v4 Async Signaling, ICE Fast Path, and JRTC Integration Upgrade`): existing code targets; async-first orchestration; trickle correctness; live handle registry/generation; non-blocking broker and JRTC reader.
- `AGENTS(20260819-174955).md`: JRTC migration command/event planes, Broka `janus.events` with `Dispio`, `jrtc` + `jrtc-video` independence, JavaScript ID-string rule and historical migration protections.
- `AGENTS-jrtc.md` (`JRTC Realtime Control-Plane Performance Upgrade`): JRTC is generic; transport receive loop must not wait on Broka/DB; bounded queues and transaction completion precedence.
- 18 Oct 2026 proposed dedicated Python ASGI signaling gateway, Django control-plane and gateway ownership; 27 Oct 2026 confirmed detailed join/admit/remove/SSE design.

## Verified partial repository inspection on 2026-10-27
- `synq:v4` `src/conf/asgi.py` uses Starlette mounting Socket.IO + Django with `lifespan=janus_runtime.lifespan` (blob `d036cd18e2d398dd370feed136d18bb0cb3d7490`).
- `synq:v4` `src/apps/meetings/realtime/namespace.py` defines `MeetingNamespace(AsyncNamespace)` for meeting presence/admission; uses Django ORM/service handlers (blob `756d5fed31b93dd6f81086e73b41f4e23afd2823`).
- `synq:v4` `src/apps/meetings/services/signaling.py` includes Django models/transaction fencing and JRTC wrapper (blob `56469cd03f55adf669f571fdadd22d5e7d6cfa14`).
- `synq:v4` `src/apps/meetings/jrtc/runtime.py` defines `JanusProcessRuntime` with `JanusSessionManager`, `JanusEventPublisher`, live handles and `VideoRoomAdapter` (blob `de9522e13d090b8ccdaa1e9973fdc5b7c2232af5`).
- `synq.js:codex/batched-ice-meeting-sounds` workspace `package.json` confirms Bun workspaces (`packages/*`, `app`) (blob `020e839481eabc540e9bec2a1800f77c3220ad2e`).
These were **limited file-range inspections**; execution agent MUST inspect complete working trees and commit SHAs rather than assuming this is a complete code audit.

## Key deliberate reinterpretation
Older instructions targeted optimized **in-process** Socket.IO→JRTC (`await adapter.trickle(...)` from Django ASGI); this extraction moves that async hot path to gateway. Preserve its batching, no-thread-bounce, low-DB-write and ownership invariants **inside gateway**. Preserve old path only during dual-path rollout. Django remains authoritative; do not treat gateway events as grant decisions.
