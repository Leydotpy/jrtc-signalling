# Gateway agent — service implementation
- Own `synq-signaling-gateway` service creation, ASGI lifespan, WebSocket protocol, ticket verifier, bounded commands, JRTC integration, lease and ownership, telemetry, integration tests.
- Do not import Django models, use `sync_to_async` around whole JRTC workflows, or recreate generic JRTC core transport.
- Lifespan: each gateway worker has explicit JRTC owner ID/epoch; own handles locally; health and readiness distinguish Janus unavailable / broker degraded; graceful drain stops new joins and waits for safe release.
- Never block receive loop on broker I/O; defer global events to bounded queue; ensure SDP/JSEP transactions remain prompt.
- All WebSocket messages validated for schema/scope/size/sequence, with secure close codes and rate limits.
- On worker restart, never resurrect stale handles; rebuild/renegotiate safely after lease/fencing check.
