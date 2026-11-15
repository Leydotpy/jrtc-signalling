"""SG-13: actual Django/PostgreSQL + Broka/Redis + Janus moderation acceptance.

Run in Synq's Python 3.14 environment with this checkout, pytest and aiortc.
Only isolated test databases and media processes are used. No browser willingly
leaves or stops its WebRTC sender in response to the moderation action.
"""

import argparse
import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--synq", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "src"), str(root / "tests"), str(args.synq.resolve() / "src")]
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conf.gateway_postgres_test_settings")
    import django

    django.setup()
    import httpx2 as httpx
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from apps.meetings.models import (
        GatewayAuthorizationOutbox,
        GatewayCommand,
        GatewayCommandOutcome,
    )
    from apps.meetings.services.gateway_dispatcher import GatewayCommandWorker
    from apps.meetings.services.lifecycle import MeetingLifecycleService
    from apps.meetings.tests.test_gateway_ownership import OwnershipFixture
    from asgiref.sync import async_to_sync, sync_to_async
    from django.core.handlers.asgi import ASGIHandler
    from django.test import TransactionTestCase
    from django.test.runner import DiscoverRunner
    from media_helpers import Browser, publish_payload
    from redis.asyncio import Redis
    from starlette.websockets import WebSocketDisconnect
    from test_janus_live import JanusServer

    from jrtc_signalling.app import create_app
    from jrtc_signalling.auth import AuthConfig, HTTPAuthority
    from jrtc_signalling.bus import BusConfig, TrustedKey, Verifier
    from jrtc_signalling.config import Settings
    from jrtc_signalling.janus_runtime import JanusConfig, JanusRuntime

    class ModerationIntegration(OwnershipFixture, TransactionTestCase):
        def exercise(self, *, broker_down, demote_host=False):
            from aioice.ice import get_host_addresses

            try:
                get_host_addresses(use_ipv4=True, use_ipv6=False)
            except PermissionError:
                interfaces = patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"])
                interfaces.start()
                self.addCleanup(interfaces.stop)
            self.intent()
            self.decision()
            guest_ticket, guest_claims = self.issue(self.guest)
            host_ticket, host_claims = self.issue()

            async def run(directory):
                url = os.environ["GATEWAY_TEST_REDIS_URL"]
                server = JanusServer(directory)
                await server.start()
                bus = BusConfig(
                    url,
                    self.owner,
                    Verifier(
                        {
                            self.bus.signer.key_id: TrustedKey(
                                "django-control", self.bus.signer.key.public_key()
                            )
                        }
                    ),
                    str(directory / "inbox.sqlite3"),
                    "https://testserver/api/v1/meetings/internal/gateway/commands/authorize/",
                    "test-service-" + "x" * 32,
                )
                runtime = JanusRuntime(
                    bus,
                    JanusConfig(
                        f"ws://127.0.0.1:{server.port}/",
                        server.secret,
                        "https://testserver/api/v1/meetings/internal/gateway/owners/",
                        (str(self.session.pk),),
                    ),
                )
                for authority in (runtime.authority, runtime.control.authority):
                    await authority.client.aclose()
                    authority.client = httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=ASGIHandler())
                    )
                config = AuthConfig(
                    "https://synq.example",
                    {"test-key": self.key.public_key()},
                    frozenset({"https://meet.example"}),
                    "https://testserver/api/v1/meetings/internal/gateway/authority/",
                    bus.authority_token,
                )
                app = create_app(
                    Settings(media_rehearsal=True, shutdown_timeout=30),
                    runtime_factory=lambda: runtime,
                    auth_config=config,
                    authority_factory=lambda config: HTTPAuthority(
                        config, httpx.AsyncClient(transport=httpx.ASGITransport(app=ASGIHandler()))
                    ),
                )
                worker = GatewayCommandWorker(replace(self.bus, redis_url=url))
                delivery_enabled = asyncio.Event()
                delivery_enabled.set()
                sweep = worker.sweep

                async def gated_sweep():
                    if delivery_enabled.is_set():
                        await sweep()

                worker.sweep = gated_sweep
                worker_task = None
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                guest = Browser(app, guest_claims, lambda _: guest_ticket["ticket"])
                host = Browser(app, host_claims, lambda _: host_ticket["ticket"])

                async def applied(action):
                    async with asyncio.timeout(12):
                        while True:
                            row = await sync_to_async(
                                lambda: GatewayAuthorizationOutbox.objects.get(
                                    session=self.session, action=action
                                )
                            )()
                            if row.status == "applied":
                                return row
                            self.assertNotIn(row.status, {"failed", "timed_out", "superseded"})
                            await asyncio.sleep(0.02)

                try:
                    async with app.router.lifespan_context(app):
                        worker_task = asyncio.create_task(worker.run())
                        admitted = await applied("participant.admit")
                        self.assertTrue((await guest.open())["ok"])
                        self.assertTrue((await host.open())["ok"])
                        pc.addTransceiver(AudioStreamTrack(), direction="sendonly")
                        await pc.setLocalDescription(await pc.createOffer())
                        payload = publish_payload()
                        payload["offer"]["sdp"] = pc.localDescription.sdp
                        sender = host if demote_host else guest
                        published = await sender.request("media.publisher.publish", payload)
                        self.assertTrue(published["ok"])
                        await pc.setRemoteDescription(
                            RTCSessionDescription(**published["result"]["jsep"])
                        )
                        async with asyncio.timeout(10):
                            while True:
                                event = await sender.receive()
                                if event.get("payload", {}).get("kind") == "webrtcup":
                                    break
                        ref = next(iter(runtime.registry.bindings.values())).ref
                        room = app.state.auth_endpoint.media.rooms[str(self.session.pk)].id
                        if demote_host:
                            await sync_to_async(
                                self.room.memberships.filter(profile=self.host).update
                            )(role="participant")
                            await asyncio.wait_for(host.task, 4)
                            self.assertNotIn(ref.token, runtime.registry.bindings)
                            self.assertFalse(runtime.registry.uncertain)
                            self.assertFalse(
                                (
                                    await runtime.management(
                                        str(self.session.pk), 1, "list_participants", room
                                    )
                                ).data.participants
                            )
                            self.assertTrue((await guest.request("session.ping"))["ok"])
                            return
                        if broker_down:
                            delivery_enabled.clear()
                            async with Redis.from_url(url) as redis:
                                await redis.execute_command("CLIENT", "PAUSE", 3000, "ALL")
                        await sync_to_async(MeetingLifecycleService.remove_participant)(
                            session=self.session,
                            actor_profile=self.host,
                            participant=self.guest_participant,
                        )
                        if not broker_down:
                            removed = await applied("participant.disconnect")
                        with self.assertRaises(WebSocketDisconnect):
                            while True:
                                await guest.receive()
                        await asyncio.wait_for(guest.task, 5)
                        self.assertNotIn(ref.token, runtime.registry.bindings)
                        self.assertFalse(runtime.registry.uncertain)
                        participants = await runtime.management(
                            str(self.session.pk), 1, "list_participants", room
                        )
                        self.assertFalse(participants.data.participants)
                        # No client close() or leave request has been issued.
                        # The remote Janus detach must terminate its DTLS peer.
                        async with asyncio.timeout(5):
                            while pc.connectionState != "closed":  # noqa: ASYNC110 - remote DTLS close
                                await asyncio.sleep(0.02)
                        self.assertTrue((await host.request("session.ping"))["ok"])
                        with self.assertRaises(WebSocketDisconnect):
                            await Browser(
                                app, guest_claims, lambda _: guest_ticket["ticket"]
                            ).open()
                        if broker_down:
                            row = await sync_to_async(
                                lambda: GatewayAuthorizationOutbox.objects.get(
                                    session=self.session, action="participant.disconnect"
                                )
                            )()
                            self.assertEqual(row.status, "pending")
                            delivery_enabled.set()
                            removed = await applied("participant.disconnect")
                        await sync_to_async(MeetingLifecycleService.end_session)(
                            session=self.session,
                            actor_profile=self.host,
                        )
                        terminated = await applied("meeting.terminate")
                        self.assertFalse(
                            (
                                await runtime.management(str(self.session.pk), 1, "exists", room)
                            ).data.exists
                        )
                        await asyncio.wait_for(host.task, 5)
                        rows = [admitted.pk, removed.pk, terminated.pk]
                        self.assertTrue(
                            all(
                                b["status"] in {"accepted", "applied"}
                                for b in await sync_to_async(
                                    lambda: list(
                                        GatewayCommandOutcome.objects.filter(
                                            command__outbox_id__in=rows
                                        ).values("status")
                                    )
                                )()
                            )
                        )
                        self.assertEqual(
                            await sync_to_async(
                                GatewayCommand.objects.filter(
                                    outbox_id__in=rows, status="applied"
                                ).count
                            )(),
                            3,
                        )
                finally:
                    await pc.close()
                    await asyncio.gather(guest.close(), host.close(), return_exceptions=True)
                    worker.stop.set()
                    if worker_task is not None:
                        worker_task.cancel()
                        await asyncio.gather(worker_task, return_exceptions=True)
                    await worker.transport.close()
                    await server.stop()

            with (
                tempfile.TemporaryDirectory() as directory,
                patch(
                    "apps.meetings.services.gateway_ownership.BusConfig.read", return_value=self.bus
                ),
            ):
                async_to_sync(run)(Path(directory))

        def test_signed_moderation_confirms_remote_removal_and_termination(self):
            self.exercise(broker_down=False)

        def test_broker_stall_cannot_keep_revoked_media_alive(self):
            self.exercise(broker_down=True)

        def test_host_role_change_invalidates_live_host_media(self):
            self.exercise(broker_down=False, demote_host=True)

    ModerationIntegration.__module__ = __name__
    globals()["ModerationIntegration"] = ModerationIntegration
    raise SystemExit(
        bool(DiscoverRunner(verbosity=1).run_tests([__name__ + ".ModerationIntegration"]))
    )


if __name__ == "__main__":
    main()
