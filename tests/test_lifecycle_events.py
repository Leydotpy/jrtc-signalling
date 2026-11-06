import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from jrtc_signalling.bus import LIFECYCLE, BusError, TrustedKey, Verifier
from jrtc_signalling.bus_transport import Streams
from jrtc_signalling.lifecycle_events import LifecyclePublisher, ObservationStore, snapshot
from jrtc_signalling.ownership import OwnershipError


async def start_spool(case, *, capacity=1024):
    publisher = LifecyclePublisher(case.config, capacity=capacity)
    # Unit tests exercise the durable boundary without a background Redis loop.
    await publisher.disk(publisher.store.start, case.config.signer.producer)
    return publisher


async def test_coalescing_reserves_terminal_capacity_and_ack_cannot_drop_it(bus_case, claims):
    publisher = await start_spool(bus_case, capacity=1)
    try:
        connection = await publisher.open(claims, 1, "trace")
        original = connection.body
        for _ in range(40):
            await connection.record(
                "participant.media_connected",
                snapshot("MEDIA_CONNECTED", audio=True),
                {"media_kind": "audio"},
            )
        assert len(publisher.store.pending()) == 1
        await connection.close()
        publisher.store.delivered(connection.stream, original)
        [(stream, final)] = publisher.store.pending()
        assert final["sequence"] == 42
        assert final["snapshot"] == snapshot("OFFLINE")
        await connection.record(
            "participant.media_connected",
            snapshot("MEDIA_CONNECTED", audio=True),
            {"media_kind": "audio"},
        )
        assert publisher.store.pending()[0][1] == final
        with pytest.raises(OwnershipError):
            await publisher.open({**claims, "participant_session_id": "new"}, 1, "trace")
        assert publisher.store.pending()[0][1] == final
        publisher.store.delivered(stream, final)
        assert not publisher.store.pending()
        publisher.store.replay()  # Repair even a dropped final broker entry.
        assert publisher.store.pending() == [(stream, final)]
    finally:
        await publisher.aclose()


async def test_restart_reconciles_live_state_and_replays_same_terminal_identity(bus_case, claims):
    first = await start_spool(bus_case)
    connection = await first.open(claims, 2, "trace")
    before = connection.body
    first.store.close()  # Simulate abrupt death, without graceful socket shutdown.
    second = await start_spool(bus_case)
    try:
        [(stream, after)] = second.store.pending()
        assert after["type"] == "gateway.recovery_failed"
        assert after["snapshot"] == snapshot("OFFLINE")
        assert after["sequence"] == before["sequence"] + 1
        assert after["generation"] == claims["generation"]
        assert after["gateway_generation"] == 2
        second.store.close()
        third = await start_spool(bus_case)
        try:
            assert third.store.pending() == [(stream, after)]
            with pytest.raises(OwnershipError):
                await third.open(claims, 2, "trace")
        finally:
            await third.aclose()
    finally:
        await second.aclose()


async def test_spool_excludes_second_writer_and_other_identity(bus_case, claims):
    publisher = await start_spool(bus_case)
    other = ObservationStore(publisher.store.path)
    try:
        with pytest.raises(BlockingIOError):
            other.start(bus_case.config.signer.producer)
        await publisher.open(claims, 1, "trace")
    finally:
        await publisher.aclose()
    with pytest.raises(OwnershipError):
        other.start("other-owner")


async def test_storage_failure_closes_admission_without_unbounded_retry_tasks(
    bus_case, claims, monkeypatch
):
    publisher = await start_spool(bus_case)
    try:

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic storage fault")

        monkeypatch.setattr(publisher.store, "write", fail)
        with pytest.raises(OwnershipError):
            await publisher.open(claims, 1, "trace")
        assert publisher.failed
        assert not publisher.connections
        assert publisher.task is None
    finally:
        await publisher.aclose()


@pytest.mark.parametrize("opening", [True, False])
async def test_cancellation_after_commit_keeps_terminal_state_and_sequence(
    bus_case, claims, monkeypatch, opening
):
    publisher = await start_spool(bus_case)
    connection = None if opening else await publisher.open(claims, 1, "trace")
    committed, release = Event(), Event()
    write = publisher.store.write

    def paused(*args, **kwargs):
        write(*args, **kwargs)
        if not committed.is_set():
            committed.set()
            assert release.wait(5)

    monkeypatch.setattr(publisher.store, "write", paused)
    work = asyncio.create_task(
        publisher.open(claims, 1, "trace")
        if opening
        else connection.record(
            "participant.media_connected",
            snapshot("MEDIA_CONNECTED", audio=True),
            {"media_kind": "audio"},
        )
    )
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        work.cancel()
        await asyncio.sleep(0)
        work.cancel()  # Repeated ASGI cancellation still joins the storage job.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        if connection:
            assert connection.body["sequence"] == 2
            await connection.close()
        [(stream, final)] = publisher.store.pending()
        assert final["snapshot"] == snapshot("OFFLINE")
        assert final["sequence"] == (2 if opening else 3)
        assert stream not in publisher.connections
    finally:
        release.set()
        await publisher.aclose()


async def test_signed_snapshots_reject_tampering_and_media_content(bus_case, claims):
    publisher = await start_spool(bus_case)
    try:
        connection = await publisher.open(claims, 1, "trace")
        signer = bus_case.config.signer
        verifier = Verifier({signer.key_id: TrustedKey(signer.producer, signer.key.public_key())})
        packet = signer.sign(connection.body, "lifecycle-event")
        assert verifier.verify(packet, "lifecycle-event")[1] == connection.body
        changed = json.loads(packet)
        changed["body"]["generation"] += 1
        with pytest.raises(BusError):
            verifier.verify(json.dumps(changed).encode(), "lifecycle-event")
        for field in ("sdp", "candidate", "ticket", "permission"):
            with pytest.raises(ValueError):
                signer.sign({**connection.body, field: "secret"}, "lifecycle-event")
    finally:
        await publisher.aclose()


@pytest.mark.skipif(not os.getenv("GATEWAY_TEST_REDIS_URL"), reason="real Redis required")
async def test_real_broker_stall_does_not_block_durable_disconnect_and_replay(bus_case, claims):
    config = replace(bus_case.config, redis_url=os.environ["GATEWAY_TEST_REDIS_URL"])
    publisher = LifecyclePublisher(config)
    await publisher.disk(publisher.store.start, config.signer.producer)
    consumer = Streams(config.redis_url, "lifecycle-test-" + uuid4().hex)
    received = asyncio.Queue()
    verifier = Verifier(
        {config.signer.key_id: TrustedKey(config.signer.producer, config.signer.key.public_key())}
    )

    async def handle(delivery):
        _, body = verifier.verify(delivery.payload, "lifecycle-event")
        await received.put(body)
        await delivery.acknowledgement.ack()

    try:
        # Other live tests use independent signing keys on this isolated DB.
        async with Redis.from_url(config.redis_url) as redis:
            await redis.delete(LIFECYCLE)
        await publisher.transport.start()
        await consumer.start()
        await consumer.subscribe(LIFECYCLE, handle)
        connection = await publisher.open(claims, 1, "trace")
        async with Redis.from_url(config.redis_url) as redis:
            await redis.execute_command("CLIENT", "PAUSE", 2500, "ALL")
        relay = asyncio.create_task(publisher.relay())
        # A stalled broker cannot hold the local commit or the event loop.
        await asyncio.wait_for(connection.close(), 1)
        with pytest.raises(TimeoutError):
            await relay
        assert publisher.store.pending()[0][1]["snapshot"] == snapshot("OFFLINE")
        await publisher.relay()
        async with asyncio.timeout(5):
            while True:
                body = await received.get()
                if body["event_id"] == connection.body["event_id"]:
                    break
        assert body["snapshot"] == snapshot("OFFLINE")
        assert not publisher.store.pending()
        # Model loss of the broker entry after its ACK, with no subsequent
        # lifecycle transition. Periodic reconciliation reuses the stable ID.
        publisher.store.replay()
        await publisher.relay()
        async with asyncio.timeout(5):
            assert (await received.get())["event_id"] == connection.body["event_id"]
    finally:
        await consumer.close()
        await publisher.transport.close()
        await publisher.aclose()
        async with Redis.from_url(config.redis_url) as redis:
            await redis.delete(LIFECYCLE)
