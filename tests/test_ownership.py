"""Ownership protocol, handle concurrency and lifecycle failure boundaries."""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jrtc.models.response import EventResponse, PluginData, WebRTCUpResponse

from jrtc_signalling.bus import BusError, Signer, TrustedKey, Verifier, canonical, parse
from jrtc_signalling.janus_runtime import JanusConfig, JanusRuntime, janus_id
from jrtc_signalling.ownership import OwnerLease, OwnershipError, validate_owner_request


class Session:
    def __init__(self, identifier=10):
        self.id, self.generation, self.ready = identifier, 1, True
        self.plugins = {}

    async def destroy(self):
        self.ready = False
        for plugin in tuple(self.plugins.values()):
            await plugin.detach()


class Plugin:
    count = 0

    def __init__(self, *, session, **kwargs):
        self.session, self.id, self.closed = session, None, False

    async def attach(self):
        await asyncio.sleep(0)
        Plugin.count += 1
        self.id = Plugin.count
        self.session.plugins[self.id] = self

    async def detach(self):
        self.session.plugins.pop(self.id, None)
        self.closed = True


@pytest.fixture
async def runtime(bus_case, monkeypatch):
    monkeypatch.setattr("jrtc_signalling.janus_runtime.VideoRoomPlugin", Plugin)
    meeting = str(uuid4())
    r = JanusRuntime(
        bus_case.config,
        JanusConfig("ws://127.0.0.1:8188/", "s" * 32, "https://synq.example/owners/", (meeting,)),
    )
    session = Session()
    manager = SimpleNamespace(
        ready=True,
        sessions=[session],
        get_session=lambda **_: session,
        start=AsyncMock(),
        stop=AsyncMock(),
    )
    r.manager = manager

    async def request(meeting, action, generation=0):
        return OwnerLease(
            meeting,
            r.owner_id,
            r.incarnation,
            generation or 1,
            asyncio.get_running_loop().time() + 29,
        )

    r.authority.request = AsyncMock(side_effect=request)
    r.control.start, r.control.drain, r.control.aclose = AsyncMock(), AsyncMock(), AsyncMock()
    await r.start()
    try:
        yield r
    finally:
        if r.cleanup is None:
            await r.aclose()


def params(runtime):
    return runtime.config.meetings[0], str(uuid4()), str(uuid4()), "publisher", 1


@pytest.mark.parametrize("bad", [True, False, "1", 0, -1, None, 1.0])
def test_internal_ids_are_strict(bad):
    with pytest.raises(OwnershipError):
        janus_id(bad)


async def test_concurrent_attach_is_one_opaque_binding(runtime):
    args = params(runtime)
    refs = await asyncio.gather(*(runtime.registry.attach(*args) for _ in range(10)))
    assert len({ref.token for ref in refs}) == len(runtime.registry.bindings) == 1
    assert len(runtime.manager.sessions[0].plugins) == 1
    ref = refs[0]
    assert type(ref.janus_handle_id) is int
    assert ref.wire_ids()["janus_handle_id"] == str(ref.janus_handle_id)
    with pytest.raises(OwnershipError):
        await runtime.registry.attach(args[0], str(uuid4()), *args[2:])


async def test_forged_owner_generation_and_handle_never_invoke(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    operation = AsyncMock()
    for forged in [
        replace(ref, owner_id="foreign"),
        replace(ref, owner_generation=2),
        replace(ref, token=str(uuid4())),
        replace(ref, participant_session_id=str(uuid4())),
    ]:
        with pytest.raises(OwnershipError):
            await runtime.registry.invoke(forged, operation)
    operation.assert_not_awaited()


async def test_expired_lease_fences_cached_handle(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    runtime.leases[ref.meeting_id] = replace(runtime.leases[ref.meeting_id], deadline=0)
    operation = AsyncMock()
    with pytest.raises(OwnershipError):
        await runtime.registry.invoke(ref, operation)
    operation.assert_not_awaited()
    assert runtime.health().ownership == "unavailable"


async def test_session_replacement_never_resurrects_handle(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    old = runtime.manager.sessions[0]
    old.ready = False
    new = Session(20)
    runtime.manager.sessions = [new]
    runtime.manager.get_session = lambda **_: new
    with pytest.raises(OwnershipError):
        await runtime.registry.invoke(ref, AsyncMock())
    await runtime.registry.prune()
    replacement = await runtime.registry.attach(
        ref.meeting_id, ref.participant_id, ref.participant_session_id, ref.role, 1
    )
    assert replacement.token != ref.token
    assert replacement.janus_session_id == 20
    with pytest.raises(OwnershipError):
        await runtime.registry.invoke(ref, AsyncMock())


async def test_detach_waits_for_operation_fence(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def operation(plugin):
        entered.set()
        await finish.wait()
        assert not plugin.closed

    running = asyncio.create_task(runtime.registry.invoke(ref, operation))
    await entered.wait()
    detach = asyncio.create_task(runtime.registry.detach(ref))
    await asyncio.sleep(0)
    assert not detach.done()
    finish.set()
    await asyncio.gather(running, detach)
    assert not runtime.registry.bindings


async def test_cancelled_operation_disposes_unknown_result(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    entered = asyncio.Event()

    async def operation(plugin):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(runtime.registry.invoke(ref, operation))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ref.token not in runtime.registry.bindings
    assert not runtime.manager.sessions[0].plugins


async def test_shutdown_disposes_handles_before_session_and_release(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    session = runtime.manager.sessions[0]
    original = session.destroy

    async def destroy():
        assert not runtime.registry.bindings
        assert not session.plugins
        await original()

    session.destroy = destroy
    await runtime.aclose()
    assert runtime.stopping.is_set()
    assert runtime.authority.request.await_args.args == (ref.meeting_id, "release", 1)
    assert not runtime.leases
    assert not runtime.registry.bindings


async def test_unclean_shutdown_does_not_release_generation(runtime):
    ref = await runtime.registry.attach(*params(runtime))
    binding = runtime.registry.bindings[ref.token]
    binding.plugin.detach = AsyncMock(side_effect=OSError("synthetic"))
    with pytest.raises(OwnershipError):
        await runtime.aclose()
    assert not any(call.args[1] == "release" for call in runtime.authority.request.await_args_list)
    runtime.manager.stop.assert_awaited_once()


async def test_only_current_routed_command_reaches_future_enforcement(runtime, bus_case):
    command = {
        **bus_case.command,
        "meeting_id": runtime.config.meetings[0],
        "expected_owner_generation": 1,
    }
    assert await runtime.apply(command) == ("failed", "unavailable")
    assert await runtime.apply({**command, "expected_owner_generation": 2}) == (
        "failed",
        "owner_lost",
    )
    assert await runtime.apply({**command, "target_owner": "elsewhere"}) == ("failed", "owner_lost")


async def test_generic_observer_cannot_publish_sdp_or_arbitrary_extras(runtime, monkeypatch):
    captured = []
    monkeypatch.setattr(
        "jrtc.messaging.JanusEventPublisher.try_admit",
        lambda self, response, **kwargs: captured.append(response.model_dump()) or True,
    )
    assert not runtime.publisher.try_admit(
        EventResponse(
            janus="event",
            sender=1,
            plugindata=PluginData(plugin="janus.plugin.videoroom", data={"sdp": "secret"}),
        )
    )
    assert runtime.publisher.try_admit(
        WebRTCUpResponse(janus="webrtcup", sender=1, sdp="secret", arbitrary="secret")
    )
    assert "secret" not in str(captured)


def owner_request():
    return {
        "request_id": str(uuid4()),
        "meeting_id": str(uuid4()),
        "incarnation": str(uuid4()),
        "action": "claim",
        "generation": 0,
        "issued_at": int(time.time()),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"generation": True},
        {"generation": -1},
        {"action": "adopt"},
        {"meeting_id": "bad"},
        {"extra": "bad"},
        {"issued_at": 1.0},
    ],
)
def test_owner_contract_is_strict(change):
    with pytest.raises(ValueError):
        validate_owner_request({**owner_request(), **change})


def test_owner_rpc_signature_binds_all_fields():
    private = Ed25519PrivateKey.generate()
    signer = Signer("owner-a", "key-a", private)
    verifier = Verifier({"key-a": TrustedKey("owner-a", private.public_key())})
    request = owner_request()
    packet = signer.sign(request, "owner-request")
    assert verifier.verify(packet, "owner-request") == ("owner-a", request)
    tampered = parse(packet)
    tampered["body"]["action"] = "release"
    with pytest.raises(BusError):
        verifier.verify(canonical(tampered), "owner-request")


async def test_lease_rpc_requires_correlated_bounded_response(runtime):
    async def handler(request):
        body = parse(request.content)["body"]
        return httpx.Response(
            200,
            json={
                **{k: body[k] for k in ("request_id", "meeting_id", "incarnation")},
                "owner_id": runtime.owner_id,
                "generation": 1,
                "lease_seconds": 999,
            },
        )

    # Restore the real method, retaining a deterministic HTTP transport.
    from jrtc_signalling.ownership import OwnershipClient

    client = OwnershipClient(
        runtime.control.config, "https://synq.example/owners/", runtime.incarnation
    )
    await client.client.aclose()
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(OwnershipError):
            await client.request(runtime.config.meetings[0], "claim")
    finally:
        await client.close()


async def test_failed_start_releases_only_claimed_resources(runtime):
    from jrtc_signalling.config import Settings
    from jrtc_signalling.runtime import Lifecycle, LifecycleError

    await runtime.aclose()
    fresh = JanusRuntime(runtime.control.config, runtime.config)
    fresh.authority.request = AsyncMock(
        return_value=OwnerLease(
            runtime.config.meetings[0],
            runtime.owner_id,
            fresh.incarnation,
            2,
            asyncio.get_running_loop().time() + 29,
        )
    )
    fresh.manager = SimpleNamespace(
        start=AsyncMock(side_effect=OSError("synthetic")),
        stop=AsyncMock(),
        sessions=[],
        ready=False,
    )
    fresh.control.aclose = AsyncMock()
    lifecycle = Lifecycle(fresh, Settings())
    with pytest.raises(LifecycleError):
        await lifecycle.start()
    assert lifecycle.phase == "failed"
    fresh.manager.stop.assert_awaited_once()
    assert not fresh.leases
    assert fresh.authority.request.await_args.args[1] == "release"


async def test_lifespan_cancellation_finishes_ordered_cleanup(runtime):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = runtime.control.aclose

    async def delayed_close():
        entered.set()
        await finish.wait()
        await original()

    runtime.control.aclose = delayed_close
    shutdown = asyncio.create_task(runtime.aclose())
    await entered.wait()
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown
    assert not runtime.cleanup.done()
    finish.set()
    await runtime.aclose()
    assert not runtime.manager.sessions[0].ready
    assert not runtime.leases


async def test_cleanup_failure_still_closes_authority_and_manager(runtime):
    runtime.control.aclose = AsyncMock(side_effect=OSError("synthetic"))
    runtime.authority.close = AsyncMock()
    with pytest.raises(OwnershipError):
        await runtime.aclose()
    runtime.authority.close.assert_awaited_once()
    runtime.manager.stop.assert_awaited_once()
    assert not any(call.args[1] == "release" for call in runtime.authority.request.await_args_list)
