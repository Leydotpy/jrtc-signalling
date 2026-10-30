"""Real Redis/Broka transport checks; CI supplies an isolated Redis 7.4 service."""

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from jrtc_signalling.bus import EVENTS, Signer, TrustedKey, Verifier, command_route
from jrtc_signalling.bus_transport import Streams
from jrtc_signalling.commands import CommandRuntime

pytestmark = pytest.mark.skipif(
    not os.getenv("GATEWAY_TEST_REDIS_URL"), reason="real Redis required"
)


async def test_real_broka_delivery_duplicate_restart_and_signed_failed_outcome(bus_case):
    url = os.environ["GATEWAY_TEST_REDIS_URL"]
    owner = "test-" + uuid4().hex
    config = replace(
        bus_case.config,
        redis_url=url,
        signer=Signer(owner, "owner-key", bus_case.config.signer.key),
    )
    bus_case.command["target_owner"] = owner
    route = command_route(owner)
    runtime = CommandRuntime(config)
    runtime.consumer.authority = SimpleNamespace(check=async_true)
    pub = Streams(url, "producer-" + uuid4().hex)
    received = asyncio.Event()
    verifier = Verifier({"owner-key": TrustedKey(owner, config.signer.key.public_key())})
    outcomes = []
    consumer = Streams(url, "outcome-test-" + uuid4().hex)

    async def receive(message):
        # Other tests may share the event stream; only consume our owner.
        packet = json.loads(message.payload)
        if packet["producer"] == owner:
            _producer, body = verifier.verify(message.payload, "command-outcome")
            outcomes.append(body)
            if body["status"] == "failed":
                received.set()
        await message.acknowledgement.ack()

    try:
        await pub.start()
        await consumer.start()
        await consumer.subscribe(EVENTS, receive)
        await runtime.start()
        packet = bus_case.signer.sign(bus_case.command, "command")
        await pub.publish(route, packet, bus_case.command["command_id"])
        await asyncio.wait_for(received.wait(), 10)
        assert {x["status"] for x in outcomes} == {"accepted", "failed"}
        assert outcomes[-1]["reason_code"] == "owner_lost"  # actual default, no mock success
        first_ids = {x["event_id"] for x in outcomes}
        await runtime.aclose()
        received.clear()
        runtime = CommandRuntime(config)
        runtime.consumer.authority = SimpleNamespace(check=async_true)
        await runtime.start()
        await pub.publish(route, packet, bus_case.command["command_id"])
        await asyncio.wait_for(received.wait(), 10)
        assert {x["event_id"] for x in outcomes} == first_ids
        assert runtime.consumer.counters["duplicate"] == 1
    finally:
        await runtime.aclose()
        await pub.close()
        await consumer.close()
        async with Redis.from_url(url) as client:
            await client.delete(route)
            await client.xgroup_destroy(EVENTS, consumer.engine.config["group"])


async def async_true(_command):
    return True


async def test_pending_message_is_reclaimed_and_slow_consumer_is_serial(bus_case):
    url = os.environ["GATEWAY_TEST_REDIS_URL"]
    owner = "test-" + uuid4().hex
    route = command_route(owner)
    group = "reclaim-" + uuid4().hex
    pub, first, second = (
        Streams(url, "pub"),
        Streams(url, group, claim_idle_ms=50),
        Streams(url, group, claim_idle_ms=50),
    )
    begun, recovered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def reserve(message):
        begun.set()  # no ACK: simulate a consumer crash before commit

    async def receive(message):
        calls.append(message)
        recovered.set()
        await release.wait()
        await message.acknowledgement.ack()

    try:
        await pub.start()
        await first.start()
        await first.subscribe(route, reserve)
        packet = bus_case.signer.sign(bus_case.command, "command")
        await pub.publish(route, packet, bus_case.command["command_id"])
        await asyncio.wait_for(begun.wait(), 5)
        await first.close()
        await second.start()
        await second.subscribe(route, receive)
        await asyncio.wait_for(recovered.wait(), 5)
        assert calls[0].transport_metadata["reclaimed"] is True
        for _ in range(12):
            await pub.publish(route, packet, bus_case.command["command_id"])
        assert len(calls) == 1
        async with Redis.from_url(url) as client:
            pending = await client.xpending(route, group)
            assert pending["pending"] == 1
        release.set()
    finally:
        release.set()
        await first.close()
        await second.close()
        await pub.close()
        async with Redis.from_url(url) as client:
            await client.delete(route)
