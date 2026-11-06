"""Real Janus + Broka/Redis + Django authority/inbox integration, in isolated tests.

Run in Synq's Python 3.14 environment with this gateway, pytest and aiortc installed.
Requires GATEWAY_TEST_REDIS_URL, Janus and conf.gateway_postgres_test_settings for
PostgreSQL acceptance. Test keys and media stay in memory or temporary directories.
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
    sys.path.insert(0, str(args.synq.resolve() / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conf.test_settings")
    import django

    django.setup()
    import httpx2 as httpx
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from apps.meetings.models import GatewaySessionObservation, Participant
    from apps.meetings.services.gateway_event_worker import GatewayEventWorker
    from apps.meetings.tests.test_gateway_ownership import OwnershipFixture
    from asgiref.sync import async_to_sync, sync_to_async
    from django.core.handlers.asgi import ASGIHandler
    from django.test import TransactionTestCase
    from django.test.runner import DiscoverRunner
    from media_helpers import Browser, publish_payload
    from redis.asyncio import Redis
    from test_janus_live import JanusServer

    from jrtc_signalling.app import create_app
    from jrtc_signalling.auth import AuthConfig, HTTPAuthority
    from jrtc_signalling.bus import LIFECYCLE, BusConfig, TrustedKey, Verifier
    from jrtc_signalling.config import Settings
    from jrtc_signalling.janus_runtime import JanusConfig, JanusRuntime

    class LifecycleIntegration(OwnershipFixture, TransactionTestCase):
        def test_actual_media_and_lobby_observations_replay_after_broker_stall(self):
            from aioice.ice import get_host_addresses

            try:
                get_host_addresses(use_ipv4=True, use_ipv6=False)
            except PermissionError:
                # Restricted local runners cannot enumerate interfaces; actual
                # media still crosses UDP to Janus on the available loopback.
                interfaces = patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"])
                interfaces.start()
                self.addCleanup(interfaces.stop)
            issued, claims = self.issue()
            self.waiting()
            lobby_ticket, lobby_claims = self.issue(self.guest)
            before = Participant.objects.values().get(pk=self.participant.pk)

            async def exercise(directory):
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
                await runtime.authority.client.aclose()
                runtime.authority.client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=ASGIHandler())
                )
                await runtime.control.authority.client.aclose()
                runtime.control.authority.client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=ASGIHandler())
                )
                auth = AuthConfig(
                    "https://synq.example",
                    {"test-key": self.key.public_key()},
                    frozenset({"https://meet.example"}),
                    "https://testserver/api/v1/meetings/internal/gateway/authority/",
                    bus.authority_token,
                )
                app = create_app(
                    Settings(media_rehearsal=True, lifecycle_events=True, shutdown_timeout=30),
                    runtime_factory=lambda: runtime,
                    auth_config=auth,
                    authority_factory=lambda config: HTTPAuthority(
                        config, httpx.AsyncClient(transport=httpx.ASGITransport(app=ASGIHandler()))
                    ),
                )
                worker = GatewayEventWorker(replace(self.bus, redis_url=url))
                task = asyncio.create_task(worker.run())
                peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))

                async def observed(participant, state):
                    async with asyncio.timeout(12):
                        while True:
                            row = await sync_to_async(
                                lambda: (
                                    GatewaySessionObservation.objects.filter(
                                        participant_id=participant
                                    )
                                    .values_list("envelope", flat=True)
                                    .first()
                                )
                            )()
                            if row and row["snapshot"]["connection"] == state:
                                return row
                            await asyncio.sleep(0.02)

                try:
                    async with app.router.lifespan_context(app):
                        browser = Browser(app, claims, lambda _: issued["ticket"])
                        lobby = Browser(app, lobby_claims, lambda _: lobby_ticket["ticket"])
                        try:
                            self.assertTrue((await lobby.open())["ok"])
                            await observed(lobby_claims["participant_id"], "LOBBY_CONNECTED")
                            self.assertFalse(runtime.registry.bindings)
                            self.assertTrue((await browser.open())["ok"])
                            await observed(claims["participant_id"], "SIGNALING_CONNECTED")
                            peer.addTransceiver(AudioStreamTrack(), direction="sendonly")
                            await peer.setLocalDescription(await peer.createOffer())
                            payload = publish_payload()
                            payload["offer"]["sdp"] = peer.localDescription.sdp
                            response = await browser.request("media.publisher.publish", payload)
                            self.assertTrue(response["ok"])
                            await peer.setRemoteDescription(
                                RTCSessionDescription(**response["result"]["jsep"])
                            )
                            connected = await observed(claims["participant_id"], "MEDIA_CONNECTED")
                            self.assertTrue(connected["snapshot"]["audio"])
                            # An actual broker outage cannot block Janus transactions
                            # or terminal persistence. Recovery delivers the snapshot.
                            async with Redis.from_url(url) as redis:
                                await redis.execute_command("CLIENT", "PAUSE", 3000, "ALL")
                            await asyncio.wait_for(
                                runtime.management(str(self.session.pk), 1, "list_rooms"), 1
                            )
                            await asyncio.wait_for(browser.close(), 1)
                            offline = await observed(claims["participant_id"], "OFFLINE")
                            self.assertGreater(offline["sequence"], connected["sequence"])
                            self.assertGreater(runtime.lifecycle.counters["retry"], 0)
                            await lobby.close()
                            await observed(lobby_claims["participant_id"], "OFFLINE")
                        finally:
                            await browser.close()
                            await lobby.close()
                finally:
                    await peer.close()
                    worker.stop.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await server.stop()
                    async with Redis.from_url(url) as redis:
                        await redis.delete(LIFECYCLE)

            with (
                tempfile.TemporaryDirectory() as directory,
                patch(
                    "apps.meetings.services.gateway_ownership.BusConfig.read", return_value=self.bus
                ),
            ):
                async_to_sync(exercise)(Path(directory))
            after = Participant.objects.values().get(pk=self.participant.pk)
            for key in (
                "status",
                "role",
                "authorization_epoch",
                "joined_at",
                "can_publish_audio",
                "can_publish_video",
                "can_share_screen",
            ):
                self.assertEqual(before[key], after[key])
            guest = Participant.objects.get(pk=lobby_claims["participant_id"])
            self.assertEqual(guest.status, "waiting")
            self.assertIsNone(guest.joined_at)

    LifecycleIntegration.__module__ = __name__
    globals()["LifecycleIntegration"] = LifecycleIntegration
    raise SystemExit(
        bool(DiscoverRunner(verbosity=1).run_tests([__name__ + ".LifecycleIntegration"]))
    )


if __name__ == "__main__":
    main()
