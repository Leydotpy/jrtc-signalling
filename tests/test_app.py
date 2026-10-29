import asyncio

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

from jrtc_signalling.app import create_app
from jrtc_signalling.config import Settings
from jrtc_signalling.runtime import (
    ComponentStatus,
    DependencyHealth,
    Lifecycle,
    LifecycleError,
    ScaffoldRuntime,
)


class ControlledRuntime(ScaffoldRuntime):
    def __init__(self):
        self.calls = []
        self.status = DependencyHealth(*(ComponentStatus.READY for _ in range(4)))

    async def start(self):
        self.calls.append("start")

    async def drain(self):
        self.calls.append("drain")

    async def aclose(self):
        self.calls.append("close")

    def health(self):
        return self.status


def test_default_process_is_live_but_never_claims_media_readiness():
    app = create_app(Settings())
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive", "phase": "running"}
        for path in ["/health", "/health/ready"]:
            response = client.get(path)
            assert response.status_code == 503
            assert response.json()["ready"] is False
            assert response.json()["signalling"] == "disabled"
            assert set(response.json()["dependencies"].values()) == {"not_configured"}
            assert response.headers["Cache-Control"] == "no-store"
    assert app.state.lifecycle.phase == "stopped"


def test_lifespan_owns_fresh_runtime_identity_and_ordered_cleanup():
    instances = []

    def factory():
        instance = ControlledRuntime()
        instances.append(instance)
        return instance

    app = create_app(Settings(), runtime_factory=factory)
    identities = []
    for _ in range(2):
        with TestClient(app):
            identities.append(app.state.lifecycle.instance_id)
        assert instances[-1].calls == ["start", "drain", "close"]
    assert identities[0] != identities[1]
    assert len(instances) == 2


def test_probes_do_not_hide_independent_dependency_failures():
    runtime = ControlledRuntime()
    runtime.status = DependencyHealth(
        janus=ComponentStatus.UNAVAILABLE,
        broka=ComponentStatus.DEGRADED,
        authority=ComponentStatus.READY,
        ownership=ComponentStatus.READY,
    )
    with TestClient(create_app(Settings(), runtime_factory=lambda: runtime)) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["dependencies"]["janus"] == "unavailable"
        assert response.json()["dependencies"]["broka"] == "degraded"


def test_even_healthy_fake_dependencies_cannot_enable_unimplemented_signalling():
    with TestClient(create_app(Settings(), runtime_factory=ControlledRuntime)) as client:
        assert client.get("/health/ready").status_code == 503
        with (
            pytest.raises(WebSocketDenialResponse) as error,
            client.websocket_connect("/ws", subprotocols=["jrtc.signalling.v1"]),
        ):
            pytest.fail("An unimplemented verifier accepted a socket")
        assert error.value.status_code == 503


@pytest.mark.parametrize("path", ["/ws", "/gateway/signalling"])
def test_configurable_ws_route_denies_before_accept_and_does_not_echo_credentials(path):
    with TestClient(create_app(Settings(websocket_path=path))) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as error,
            client.websocket_connect(
                path + "?ticket=private-query", headers={"origin": "https://untrusted.test"}
            ),
        ):
            pytest.fail("Unexpected upgrade")
        assert error.value.status_code == 503
        assert "private-query" not in error.value.text
        assert error.value.json()["error"]["code"] == "UNAVAILABLE"
        assert client.get(path).status_code == 404


async def test_ws_denial_without_asgi_http_extension():
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    app = create_app(Settings())
    await app(
        {
            "type": "websocket",
            "path": "/ws",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "scheme": "ws",
        },
        receive,
        send,
    )
    assert messages == [
        {"type": "websocket.close", "code": 1008, "reason": "Signalling is not enabled."}
    ]


def test_missing_lifespan_never_claims_liveness():
    client = TestClient(create_app(Settings()))
    assert client.get("/health/live").status_code == 503
    assert client.get("/health/ready").status_code == 503


async def test_startup_failure_closes_partial_resources_with_safe_error():
    class FailedRuntime(ControlledRuntime):
        async def start(self):
            self.calls.append("start")
            raise RuntimeError("private-token-in-adapter-error")

    runtime = FailedRuntime()
    lifecycle = Lifecycle(runtime, Settings())
    with pytest.raises(LifecycleError) as error:
        await lifecycle.start()
    assert runtime.calls == ["start", "drain", "close"]
    assert lifecycle.phase == "failed"
    assert "private-token" not in str(error.value)


async def test_startup_timeout_releases_partial_resources():
    class StalledRuntime(ControlledRuntime):
        async def start(self):
            self.calls.append("start")
            await asyncio.Event().wait()

    runtime = StalledRuntime()
    lifecycle = Lifecycle(runtime, Settings(startup_timeout=0.01))
    with pytest.raises(LifecycleError):
        await lifecycle.start()
    assert runtime.calls == ["start", "drain", "close"]


async def test_startup_cancellation_waits_for_owned_cleanup():
    started = asyncio.Event()

    class StalledRuntime(ControlledRuntime):
        async def start(self):
            self.calls.append("start")
            started.set()
            await asyncio.Event().wait()

    runtime = StalledRuntime()
    lifecycle = Lifecycle(runtime, Settings())
    task = asyncio.create_task(lifecycle.start())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.calls == ["start", "drain", "close"]


async def test_shutdown_is_shared_and_caller_cancellation_does_not_abandon_close():
    draining, release = asyncio.Event(), asyncio.Event()

    class SlowRuntime(ControlledRuntime):
        async def drain(self):
            self.calls.append("drain")
            draining.set()
            await release.wait()

    runtime = SlowRuntime()
    lifecycle = Lifecycle(runtime, Settings())
    await lifecycle.start()
    first = asyncio.create_task(lifecycle.stop())
    await draining.wait()
    assert lifecycle.phase == "draining"
    first.cancel()
    await asyncio.sleep(0)  # Deliver cancellation while drain remains blocked.
    first.cancel()
    second = asyncio.create_task(lifecycle.stop())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    await lifecycle.stop()
    assert runtime.calls == ["start", "drain", "close"]
    assert lifecycle.phase == "stopped"


async def test_cancelled_caller_preserves_cancellation_when_cleanup_fails():
    draining, release = asyncio.Event(), asyncio.Event()

    class FailedRuntime(ControlledRuntime):
        async def drain(self):
            draining.set()
            await release.wait()

        async def aclose(self):
            raise RuntimeError("private-error")

    lifecycle = Lifecycle(FailedRuntime(), Settings())
    await lifecycle.start()
    task = asyncio.create_task(lifecycle.stop())
    await draining.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lifecycle.phase == "failed"


@pytest.mark.parametrize("fail_phase", ["drain", "close"])
async def test_shutdown_errors_remain_visible_and_safe(fail_phase):
    class FailedRuntime(ControlledRuntime):
        async def drain(self):
            self.calls.append("drain")
            if fail_phase == "drain":
                raise RuntimeError("private-credential")

        async def aclose(self):
            self.calls.append("close")
            if fail_phase == "close":
                raise RuntimeError("private-credential")

    runtime = FailedRuntime()
    lifecycle = Lifecycle(runtime, Settings())
    await lifecycle.start()
    with pytest.raises(LifecycleError) as error:
        await lifecycle.stop()
    assert runtime.calls == ["start", "drain", "close"]
    assert lifecycle.phase == "failed"
    assert "private-credential" not in str(error.value)


async def test_shutdown_deadline_does_not_claim_success():
    class StalledRuntime(ControlledRuntime):
        async def aclose(self):
            self.calls.append("close")
            await asyncio.Event().wait()

    lifecycle = Lifecycle(StalledRuntime(), Settings(shutdown_timeout=0.01))
    await lifecycle.start()
    with pytest.raises(LifecycleError):
        await lifecycle.stop()
    assert lifecycle.phase == "failed"


def test_broken_health_provider_fails_closed():
    class BrokenRuntime(ControlledRuntime):
        def health(self):
            raise RuntimeError("private-broker-url")

    with TestClient(create_app(Settings(), runtime_factory=BrokenRuntime)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert set(response.json()["dependencies"].values()) == {"unavailable"}
        assert "private-broker-url" not in response.text
