# JRTC signalling

Dedicated Python ASGI signalling service for Synq. Django remains the authority
for identity, admission, roles and revocation. JRTC and VideoRoom will manage
Janus sessions in this separately deployed process; browser media goes directly
to Janus.

SG-00–SG-04 provide the service scaffold, wire contracts, Django ticket authority
and gateway authentication. **Live media signalling is not enabled.** Authenticated
control sessions can be rehearsed with explicit TLS/auth configuration; readiness
remains 503 until durable commands and exclusive JRTC ownership are integrated.
Existing Django/browser signalling remains unchanged.

See [progress and blockers](docs/implementation/PROGRESS.md),
[v1 protocol and compatibility map](docs/PROTOCOL.md),
[ticket security contract](docs/SECURITY.md),
[authentication setup](docs/AUTHENTICATION.md) and the
[instruction pack](docs/implementation/README.md).

## Run locally

Python 3.12–3.14 and uv 0.13.4:

```sh
uv sync --locked --group dev
uv run jrtc-signalling --check-config
uv run jrtc-signalling
```

Default listener: `127.0.0.1:8765`. `python -m jrtc_signalling` is equivalent.
Use these entrypoints to retain safe logging and WebSocket server limits.
Settings are read from `JRTC_SIGNALLING_*` environment variables; see
[.env.example](.env.example). Dotenv files are not loaded automatically.
No Django settings, Janus URL or broker credentials are needed for the scaffold.
There is deliberately no environment switch to enable unimplemented media access.

| Endpoint | Scaffold response | Purpose |
| --- | --- | --- |
| GET /health/live | 200 while running/draining | process liveness |
| GET /health/ready | 503, dependencies and signalling=disabled | traffic readiness |
| GET /health | same as readiness | aggregate status |
| WS /ws | Disabled by default; configured WSS accepts authenticated control sessions | authentication rehearsal |

The gateway reports Janus, Broka, authority and ownership independently.
No real connections are made by ScaffoldRuntime. Instance UUIDs are diagnostic;
they are not distributed lease/generation fences. SIGTERM drains then closes
owned resources within the configured cooperative timeout. Lifecycle failure
does not masquerade as successful shutdown.

Logs are JSON with safe event names, level and timestamp. Raw messages, URL
queries, arguments and tracebacks are excluded, including Uvicorn's handshake
logs. Ingress/proxy logging must independently omit credentials and query strings.

## Verification

```sh
uv run ruff check src tests scripts/export_schemas.py
uv run ruff format --check src tests scripts/export_schemas.py
uv run mypy
uv run python scripts/export_schemas.py --check
uv run pytest -q
uv build
```

Tests cover wire compatibility, invalid inputs, lifecycle failures/cancellation,
dependency degradation, configuration, log redaction, real HTTP/HTTPS/WS/WSS processes,
and SIGTERM. Signature/origin/replay/refresh/revocation and bounded authority tests
cover SG-04. GitHub Actions runs Python 3.12/3.13/3.14, container smoke and a pinned
Synq issuer/authority handoff check. Live Janus/Broka/browser-media acceptance remains later work.

## Local container

```sh
docker compose up --build
# Optional local source mount; restart gateway after edits:
docker compose -f compose.yaml -f compose.dev.yaml up --build
```

Compose binds only localhost, drops capabilities and uses a read-only filesystem.
The image runs as a non-root user. Its healthcheck probes liveness; an image being
healthy does **not** mean it can serve media. No production service, secrets or
deployment has been created.

## Next dependency

SG-05 adds the gateway admission/outbox path in Synq; SG-06 delivers durable
commands, followed by SG-07/08 runtime ownership and media dispatch. Follow the
[task order](docs/implementation/EXECUTION_ORDER.md).
