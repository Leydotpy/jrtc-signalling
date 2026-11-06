"""Opt-in tests against an isolated, actual Janus process and Redis."""

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx
import pytest
from jrtc_video import VideoRoomCreateRequest, VideoRoomDestroyRequest
from media_helpers import Browser, publish_payload

from jrtc_signalling.app import create_app
from jrtc_signalling.config import Settings
from jrtc_signalling.janus_runtime import JanusConfig, JanusRuntime
from jrtc_signalling.ownership import OwnerLease, OwnershipError

pytestmark = pytest.mark.skipif(
    os.environ.get("GATEWAY_TEST_JANUS") != "1", reason="isolated real Janus required"
)


class JanusServer:
    def __init__(self, directory: Path):
        self.directory = directory
        self.secret = secrets.token_hex(32)
        self.process = None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        library_root = Path(os.environ.get("GATEWAY_TEST_JANUS_LIB_DIR", "/usr/lib"))
        for name, library in [
            ("plugins", "libjanus_videoroom.so"),
            ("transports", "libjanus_websockets.so"),
        ]:
            folder = directory / name
            folder.mkdir()
            source = next(library_root.rglob(library))
            (folder / library).symlink_to(source)
        (directory / "events").mkdir()
        (directory / "janus.jcfg").write_text(
            "general: {\n"
            f'configs_folder = "{directory}"\n'
            f'plugins_folder = "{directory / "plugins"}"\n'
            f'transports_folder = "{directory / "transports"}"\n'
            f'events_folder = "{directory / "events"}"\n'
            f'api_secret = "{self.secret}"\n'
            "debug_level = 0\ndebug_colors = false\nsession_timeout = 15\n"
            "reclaim_session_timeout = 0\n}\nevents: { broadcast = false }\n"
        )
        (directory / "janus.transport.websockets.jcfg").write_text(
            "general: {\nws = true\n"
            f'ws_port = {self.port}\nws_ip = "127.0.0.1"\nwss = false\n'
            "}\nadmin: { admin_ws = false\nadmin_wss = false }\n"
        )
        (directory / "janus.plugin.videoroom.jcfg").write_text("general: { }\n")

    async def start(self):
        assert shutil.which("janus"), "Install Janus to run its opted-in tests"
        self.process = await asyncio.to_thread(
            subprocess.Popen,
            [
                "janus",
                "--configs-folder",
                str(self.directory),
                "--config",
                str(self.directory / "janus.jcfg"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            assert self.process.poll() is None, "Isolated Janus failed to start"
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise AssertionError("Isolated Janus did not listen")

    async def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, timeout=5)
            self.process = None


@pytest.fixture
async def live_runtime(tmp_path, bus_case, auth_config, authority):
    server = JanusServer(tmp_path)
    await server.start()
    meeting = str(uuid4())
    bus = replace(bus_case.config, redis_url=os.environ["GATEWAY_TEST_REDIS_URL"])
    runtime = JanusRuntime(
        bus,
        JanusConfig(
            f"ws://127.0.0.1:{server.port}/",
            server.secret,
            "https://authority.example/owners/",
            (meeting,),
        ),
    )

    async def lease(meeting, action, generation=0):
        return OwnerLease(
            meeting,
            runtime.owner_id,
            runtime.incarnation,
            generation or 1,
            asyncio.get_running_loop().time() + 29,
        )

    # Django's actual signed lease API is separately covered by PostgreSQL and
    # cross-repository tests. This isolates the real Janus/SDK/ASGI lifecycle.
    runtime.authority.request = AsyncMock(side_effect=lease)
    app = create_app(
        Settings(shutdown_timeout=30, media_rehearsal=True, lifecycle_events=True),
        runtime_factory=lambda: runtime,
        auth_config=auth_config,
        authority_factory=lambda config: authority,
    )
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                assert (await client.get("/health/live")).status_code == 200
                ready = await client.get("/health/ready")
                assert ready.status_code == 503
                assert ready.json()["dependencies"]["janus"] == "ready"
                assert ready.json()["dependencies"]["ownership"] == "ready"
            yield runtime, server, meeting, app
    finally:
        await server.stop()


async def test_real_janus_session_management_and_unique_handle_disposal(live_runtime):
    runtime, _server, meeting, _app = live_runtime
    assert runtime.manager.ready
    session = runtime.session(meeting)
    assert type(session.id) is int
    room = secrets.randbelow(2**40) + 1
    await runtime.management(meeting, 1, "create", VideoRoomCreateRequest(room=room, publishers=4))
    try:
        await runtime.management(meeting, 1, "list_rooms")
        service = next(iter(runtime.services.values()))
        await runtime.management(meeting, 1, "list_rooms")
        assert next(iter(runtime.services.values())) is service
        participant, connection = str(uuid4()), str(uuid4())
        refs = await asyncio.gather(
            *(
                runtime.registry.attach(meeting, participant, connection, "publisher", 1)
                for _ in range(8)
            )
        )
        assert len({ref.token for ref in refs}) == 1
        ref = refs[0]
        assert session.plugins.get(ref.janus_handle_id) is not None
        await runtime.registry.detach(ref)
        assert session.plugins.get(ref.janus_handle_id) is None
        with pytest.raises(OwnershipError):
            await runtime.registry.invoke(ref, AsyncMock())
    finally:
        await runtime.management(meeting, 1, "destroy", VideoRoomDestroyRequest(room=room))


async def test_real_janus_loss_recovery_uses_fresh_session_and_handle(live_runtime):
    runtime, server, meeting, _app = live_runtime
    await runtime.management(meeting, 1, "list_rooms")
    old_service = next(iter(runtime.services.values()))
    ref = await runtime.registry.attach(meeting, str(uuid4()), str(uuid4()), "publisher", 1)
    old_session = runtime.session(meeting)
    await server.stop()
    deadline = asyncio.get_running_loop().time() + 10
    while old_session.ready and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110 - external process state
        await asyncio.sleep(0.05)
    assert not old_session.ready
    with pytest.raises(OwnershipError):
        await runtime.registry.invoke(ref, AsyncMock())
    await server.start()
    deadline = asyncio.get_running_loop().time() + 15
    while (  # noqa: ASYNC110 - external manager readiness has no awaitable API
        not runtime.manager.ready or runtime.session(meeting) is old_session
    ) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert runtime.manager.ready
    assert runtime.session(meeting) is not old_session
    await runtime.registry.prune()
    new = await runtime.registry.attach(
        meeting, ref.participant_id, ref.participant_session_id, "publisher", 1
    )
    assert new.token != ref.token and new.janus_session_id != ref.janus_session_id
    await runtime.management(meeting, 1, "list_rooms")
    assert any(service is not old_service for service in runtime.services.values())
    assert runtime.manager.metrics["recoveries_completed"] >= 1


async def test_real_janus_loss_records_terminal_recovery_snapshot(live_runtime, claims, sign):
    runtime, server, meeting, app = live_runtime
    browser = Browser(
        app,
        {
            **claims,
            "meeting_id": meeting,
            "participant_id": str(uuid4()),
            "participant_session_id": str(uuid4()),
        },
        sign,
    )
    try:
        assert (await browser.open())["ok"]
        observation = next(iter(runtime.lifecycle.connections.values()))
        await server.stop()
        await asyncio.wait_for(browser.task, 8)
        assert observation.closed
        assert observation.body["type"] == "gateway.recovery_failed"
        assert observation.body["snapshot"]["connection"] == "OFFLINE"
        assert observation.body["generation"] == claims["generation"]
        assert observation.body["gateway_generation"] == 1
    finally:
        if server.process is None:
            await server.start()
        await browser.close()
        # SDK replacement is asynchronous; finish this external-process
        # recovery before asking the lifespan to destroy its new session.
        async with asyncio.timeout(15):
            while not runtime.manager.ready:  # noqa: ASYNC110 - SDK readiness has no awaitable
                await asyncio.sleep(0.02)


@pytest.mark.parametrize("path", ["embedded", "trickle", "relay"])
async def test_real_janus_publisher_subscriber_offer_answer_and_cleanup(
    live_runtime, claims, sign, monkeypatch, tmp_path, path
):
    # Optional test dependency only; the gateway never terminates WebRTC media.
    from aioice.ice import get_host_addresses
    from aiortc import (
        AudioStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from live_ice_helpers import (
        TurnServer,
        apply_remote_ice,
        observe_restart_checks,
        relay_only,
        rotate_local_ice,
        split_trickle,
        trickle,
    )

    # Use the same local interfaces Janus can see. Restricted cloud runners may
    # prohibit enumeration and expose only loopback.
    def local_addresses(**kwargs):
        try:
            return get_host_addresses(**kwargs) or ["127.0.0.1"]
        except PermissionError:
            return ["127.0.0.1"]

    monkeypatch.setattr("aioice.ice.get_host_addresses", local_addresses)
    # Janus uses real host interfaces on normal runners; a loopback relay can
    # fail its candidate pairing there. Keep the TURN listener private while
    # allocating relays on an interface shared with the local Janus process.
    turn = TurnServer(tmp_path, local_addresses(use_ipv4=True, use_ipv6=False)[0])
    servers = []
    if path == "relay":
        await turn.start()
        servers = [
            RTCIceServer(
                urls=f"turn:127.0.0.1:{turn.port}?transport=udp",
                username=turn.username,
                credential=turn.password,
            )
        ]

    runtime, _server, meeting, app = live_runtime
    browsers = [
        Browser(
            app,
            {
                **claims,
                "meeting_id": meeting,
                "participant_id": str(uuid4()),
                "participant_session_id": str(uuid4()),
                "jti": str(uuid4()),
            },
            sign,
        )
        for _ in range(2)
    ]
    publisher, subscriber = browsers
    publisher_pc, subscriber_pc = [
        RTCPeerConnection(RTCConfiguration(iceServers=servers)) for _ in range(2)
    ]
    try:
        for browser in browsers:
            assert (await browser.open())["result"]["gateway_owner_id"] == runtime.owner_id
        publisher_pc.addTransceiver(AudioStreamTrack(), direction="sendonly")
        if path == "relay":
            relay_only(publisher_pc)
        await publisher_pc.setLocalDescription(await publisher_pc.createOffer())
        payload = publish_payload()
        payload["offer"]["sdp"] = publisher_pc.localDescription.sdp
        if path != "embedded":
            payload["offer"]["sdp"], pub_candidates = split_trickle(payload["offer"]["sdp"])
            if path == "relay":
                assert all(" typ relay" in c["candidate"] for c in pub_candidates)
        published = (await publisher.request("media.publisher.publish", payload))["result"]
        assert published["jsep"]["type"] == "answer"
        await publisher_pc.setRemoteDescription(RTCSessionDescription(**published["jsep"]))
        if path != "embedded":
            await trickle(publisher, "publisher", published, pub_candidates)
        async with asyncio.timeout(10):
            while True:
                event = await publisher.receive()
                if event.get("payload", {}).get("kind") == "webrtcup":
                    break
        joined = (await subscriber.request("media.subscriber.join"))["result"]
        assert joined["jsep"]["type"] == "offer"
        assert joined["selected_streams"][0]["feed"] == published["streams"][0]["janus_feed_id"]
        await subscriber_pc.setRemoteDescription(RTCSessionDescription(**joined["jsep"]))
        if path == "relay":
            relay_only(subscriber_pc)
        await subscriber_pc.setLocalDescription(await subscriber_pc.createAnswer())
        answer = subscriber_pc.localDescription.sdp
        if path != "embedded":
            answer, sub_candidates = split_trickle(answer)
            if path == "relay":
                assert all(" typ relay" in c["candidate"] for c in sub_candidates)
        started = await subscriber.request(
            "media.subscriber.start",
            {
                "answer": {"type": "answer", "sdp": answer},
                "negotiation_id": joined["negotiation_id"],
            },
        )
        assert started["result"]["action"] == "start"
        if path != "embedded":
            await trickle(subscriber, "subscriber", joined, sub_candidates)
        # Media travels directly between the test peers, Janus and (for relay)
        # coturn. SDP has no embedded candidates in either trickle case.
        track = subscriber_pc.getReceivers()[0].track
        frame = await asyncio.wait_for(track.recv(), 10)
        assert frame.samples > 0
        # Receive-only audio is an audio connection, with no publishing flags.
        observation = next(
            c
            for c in runtime.lifecycle.connections.values()
            if c.body["participant_session_id"] == subscriber.claims["participant_session_id"]
        )
        async with asyncio.timeout(5):
            while observation.body["snapshot"]["connection"] != "MEDIA_CONNECTED":  # noqa: ASYNC110 - off-loop durable commit
                await asyncio.sleep(0.01)
        assert observation.body["data"]["media_kind"] == "audio"
        assert not any(observation.body["snapshot"][key] for key in ("audio", "video", "screen"))
        if path != "embedded":
            handles_before = set(runtime.registry.bindings)
            for role, browser, pc in [
                ("publisher", publisher, publisher_pc),
                ("subscriber", subscriber, subscriber_pc),
            ]:
                restarted = (await browser.request("media.ice_restart", {"handle_type": role}))[
                    "result"
                ]
                connection = rotate_local_ice(pc)
                checks = observe_restart_checks(connection, monkeypatch)
                if role == "publisher":
                    await pc.setLocalDescription(await pc.createOffer())
                    offer, candidates = split_trickle(pc.localDescription.sdp)
                    new_payload = publish_payload(negotiation=restarted["negotiation_id"])
                    new_payload["offer"]["sdp"] = offer
                    new_result = await browser.request("media.publisher.configure", new_payload)
                    assert new_result["ok"], new_result.get("error")
                    remote = new_result["result"]["jsep"]
                    apply_remote_ice(pc, remote["sdp"])
                    await pc.setRemoteDescription(RTCSessionDescription(**remote))
                else:
                    remote = restarted["jsep"]
                    apply_remote_ice(pc, remote["sdp"])
                    await pc.setRemoteDescription(RTCSessionDescription(**remote))
                    await pc.setLocalDescription(await pc.createAnswer())
                    answer, candidates = split_trickle(pc.localDescription.sdp)
                    new_result = await browser.request(
                        "media.subscriber.start",
                        {
                            "answer": {"type": "answer", "sdp": answer},
                            "negotiation_id": restarted["negotiation_id"],
                        },
                    )
                    assert new_result["ok"], new_result.get("error")
                await trickle(browser, role, restarted, candidates)
                await asyncio.wait_for(checks.wait(), 10)
                if path == "relay":
                    assert all(" typ relay" in c["candidate"] for c in candidates)
                    assert connection._nominated
                    assert all(
                        pair.local_candidate.type == "relay"
                        for pair in connection._nominated.values()
                    )
            assert set(runtime.registry.bindings) == handles_before
            # Drain beyond pre-restart buffering and confirm continued RTP.
            for _ in range(30):
                frame = await asyncio.wait_for(track.recv(), 10)
            assert frame.samples > 0
        assert (await subscriber.request("media.subscriber.detach"))["result"]["action"] == "detach"
        assert (await publisher.request("media.publisher.unpublish"))["result"][
            "action"
        ] == "unpublish"
        assert (await publisher.request("media.publisher.publish", publish_payload()))["result"][
            "action"
        ] == "publish"
        assert (await publisher.request("media.hangup", {"handle_type": "publisher"}))["result"][
            "lifecycle_state"
        ] == "detached"
        assert not runtime.registry.bindings
    finally:
        for browser in browsers:
            await browser.close()
        await asyncio.gather(publisher_pc.close(), subscriber_pc.close())
        await turn.close()
