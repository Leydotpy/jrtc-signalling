# Repo-specific agent overlay map

| Repo | Primary concern | Existing files to inspect (hints, not guaranteed) |
| --- | --- | --- |
| `synq:v4` | Django identity/grants, outbox commands, event consumption, SSE projection, legacy cutover | `src/conf/asgi.py`, `src/apps/meetings/realtime/namespace.py`, `src/apps/meetings/services/signaling.py`, `services/janus.py`, `src/apps/meetings/jrtc/runtime.py`, `handles.py`, `videoroom/adapter.py`, tests |
| dedicated `synq-signaling-gateway` | new deployable ASGI WebSocket process, ticket verification, Broka consumers/publishers, JRTC owner, protocol | SG-02 scaffold decides layout; must not depend on Django ORM |
| `synq.js:codex/batched-ice-meeting-sounds` | SDK transport adapter, dual-path feature flag, SSE state reconciliation, ICE safety | discover current `packages/client`, `packages/react-sdk`, `packages/react-bindings`, `app` and tests |
| `jrtc:main` | generic async transport and Janus session/transaction operations | use current `src/jrtc/transport`, `messaging`, `session` and tests; avoid unnecessary edits |
| `jrtc-plugins:main` | VideoRoom operations | `jrtc-video`; preserve reusable adapter, lifecycle guarantees |

All agents must read current root AGENTS.md, inspect baseline, and not copy this pack over them. Cross-repo dependencies are handled by semantically versioned contracts and compatible commits; no blanket branch rewriting.
