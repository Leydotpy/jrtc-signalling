"""Opt-in tests against an isolated, actual Janus process and Redis."""

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jrtc_video import VideoRoomCreateRequest, VideoRoomDestroyRequest

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
        for name, library in [
            ("plugins", "libjanus_videoroom.so"),
            ("transports", "libjanus_websockets.so"),
        ]:
            folder = directory / name
            folder.mkdir()
            source = next(Path("/usr/lib").rglob(library))
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
async def live_runtime(tmp_path, bus_case):
    server = JanusServer(tmp_path)
    await server.start()
    meeting = str(uuid4())
    bus = bus_case.config
    object.__setattr__(bus, "redis_url", os.environ["GATEWAY_TEST_REDIS_URL"])
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
    try:
        await runtime.start()
        yield runtime, server, meeting
    finally:
        try:
            await runtime.aclose()
        finally:
            await server.stop()


async def test_real_janus_session_management_and_unique_handle_disposal(live_runtime):
    runtime, _server, meeting = live_runtime
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
    runtime, server, meeting = live_runtime
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
