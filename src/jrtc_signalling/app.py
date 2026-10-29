"""Standalone ASGI factory. Media access is closed until SG-04 and SG-08."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .config import Settings
from .runtime import DependencyHealth, Lifecycle, Runtime, ScaffoldRuntime


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: Callable[[], Runtime] = ScaffoldRuntime,
) -> Starlette:
    selected = settings if settings is not None else Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        lifecycle = Lifecycle(runtime_factory(), selected)
        app.state.lifecycle = lifecycle
        await lifecycle.start()
        try:
            yield
        finally:
            await lifecycle.stop()

    def snapshot(request: Request) -> dict[str, Any]:
        lifecycle: Lifecycle | None = request.app.state.lifecycle
        phase = lifecycle.phase if lifecycle else "starting"
        dependencies = lifecycle.health() if lifecycle else DependencyHealth()
        return {
            "phase": phase,
            "dependencies": dependencies.as_dict(),
            "signalling": "disabled",
            "ready": False,
        }

    async def live(request: Request) -> JSONResponse:
        lifecycle: Lifecycle | None = request.app.state.lifecycle
        phase = lifecycle.phase if lifecycle else "starting"
        alive = phase in {"running", "draining"}
        return JSONResponse(
            {"status": "alive" if alive else "not_alive", "phase": phase},
            status_code=200 if alive else 503,
            headers={"Cache-Control": "no-store"},
        )

    async def ready(request: Request) -> JSONResponse:
        # No setting can turn the incomplete verifier/runtime into an accepting service.
        return JSONResponse(
            {"status": "not_ready", **snapshot(request)},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    async def websocket_unavailable(websocket: WebSocket) -> None:
        if "websocket.http.response" in websocket.scope.get("extensions", {}):
            await websocket.send_denial_response(
                JSONResponse(
                    {"error": {"code": "UNAVAILABLE", "message": "Signalling is not enabled."}},
                    status_code=503,
                    headers={"Cache-Control": "no-store", "Retry-After": "30"},
                )
            )
        else:
            # Before accept this becomes an HTTP denial, not an authenticated socket.
            await websocket.close(code=1008, reason="Signalling is not enabled.")

    app = Starlette(
        debug=False,
        routes=[
            Route("/health/live", live),
            Route("/health/ready", ready),
            Route("/health", ready),
            WebSocketRoute(selected.websocket_path, websocket_unavailable),
        ],
        lifespan=lifespan,
    )
    app.state.lifecycle = None
    return app
