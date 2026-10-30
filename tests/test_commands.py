import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2 as httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jrtc_signalling.bus import BusConfig, BusError, Signer, TrustedKey, Verifier, canonical
from jrtc_signalling.command_store import CommandStore, StoreFull
from jrtc_signalling.commands import CommandConsumer, CommandRuntime, HTTPCommandAuthority


def delivery(case, command=None):
    return SimpleNamespace(
        payload=case.signer.sign(command or case.command, "command"),
        acknowledgement=SimpleNamespace(ack=AsyncMock()),
    )


def handler(case, *, authorized=True, apply=None):
    enforcer = (
        SimpleNamespace(apply=AsyncMock(return_value=("applied", "normal")))
        if apply is None
        else apply
    )
    consumer = CommandConsumer(
        case.config,
        case.store,
        SimpleNamespace(check=AsyncMock(return_value=authorized)),
        enforcer,
        clock=lambda: case.now,
    )
    return consumer, enforcer


@pytest.mark.parametrize(
    "mutation", ["signature", "producer", "key_id", "version", "body", "extra", "kind"]
)
def test_signature_binds_identity_kind_and_every_command_field(bus_case, mutation):
    packet = json.loads(bus_case.signer.sign(bus_case.command, "command"))
    changes = {
        "signature": "AAAA",
        "producer": "attacker",
        "key_id": "unknown",
        "version": True,
        "body": {**packet["body"], "authorization_epoch": 3},
        "extra": "private-data",
        "kind": "command-outcome",
    }
    packet[mutation] = changes[mutation]
    with pytest.raises(BusError) as error:
        bus_case.config.verifier.verify(canonical(packet), "command")
    assert "private-data" not in str(error.value)


@pytest.mark.parametrize(
    "packet", [b"[]", b"{" + b" " * 33000, b'{"version":1,"version":1}', b'{"value":NaN}', b"\xff"]
)
def test_packet_limits_and_duplicate_fields(bus_case, packet):
    with pytest.raises(BusError):
        bus_case.config.verifier.verify(packet, "command")


async def test_duplicate_across_restart_applies_once_and_republishes_outcome(bus_case):
    consumer, enforcer = handler(bus_case)
    message = delivery(bus_case)
    await consumer.handle(message)
    message.acknowledgement.ack.assert_awaited_once()
    assert bus_case.store.inspect()["pending_outcomes"] == 2
    for row in bus_case.store.pending(bus_case.now):
        bus_case.store.published(row["id"])
    restarted = CommandStore(bus_case.config.inbox_path)
    other, other_enforcer = handler(bus_case)
    other.store = restarted
    await other.handle(delivery(bus_case))
    enforcer.apply.assert_awaited_once()
    other_enforcer.apply.assert_not_awaited()
    assert restarted.inspect()["pending_outcomes"] == 2


async def test_conflicting_id_is_quarantined_without_replacing_original(bus_case):
    consumer, enforcer = handler(bus_case)
    await consumer.handle(delivery(bus_case))
    conflicting = {**bus_case.command, "authorization_epoch": 3}
    await consumer.handle(delivery(bus_case, conflicting))
    enforcer.apply.assert_awaited_once()
    assert bus_case.store.inspect()["quarantined"] == 1
    assert all(
        json.loads(row["body"])["authorization_epoch"] == 2
        for row in bus_case.store.pending(bus_case.now)
    )


@pytest.mark.parametrize("case", ["expired", "future", "revoked", "authority_down", "no_owner"])
async def test_failure_never_applies_or_reports_success(bus_case, case):
    consumer, enforcer = handler(bus_case, authorized=case != "revoked")
    if case == "expired":
        bus_case.now += 61
    if case == "future":
        bus_case.now -= 10
    if case == "authority_down":
        consumer.authority.check.side_effect = OSError("secret endpoint")
    if case == "no_owner":
        consumer = CommandConsumer(
            bus_case.config, bus_case.store, consumer.authority, clock=lambda: bus_case.now
        )
    await consumer.handle(delivery(bus_case))
    enforcer.apply.assert_not_awaited()
    events = [json.loads(row["body"]) for row in bus_case.store.pending(bus_case.now + 100)]
    assert {event["status"] for event in events} == {"accepted", "failed"}


async def test_crash_after_reservation_requires_reconciliation_not_reexecution(bus_case):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_command):
        started.set()
        await release.wait()
        return "applied", "normal"

    consumer, _ = handler(bus_case, apply=SimpleNamespace(apply=slow))
    message = delivery(bus_case)
    task = asyncio.create_task(consumer.handle(message))
    await asyncio.wait_for(started.wait(), 2)
    duplicate = delivery(bus_case)
    await consumer.handle(duplicate)
    duplicate.acknowledgement.ack.assert_not_awaited()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    message.acknowledgement.ack.assert_not_awaited()
    bus_case.now += 6
    restarted, enforcer = handler(bus_case)
    await restarted.handle(delivery(bus_case))
    enforcer.apply.assert_not_awaited()
    assert any(
        json.loads(row["body"])["status"] == "timed_out"
        for row in bus_case.store.pending(bus_case.now)
    )


async def test_ack_failure_and_storage_pressure_keep_delivery_recoverable(bus_case):
    consumer, enforcer = handler(bus_case)
    first = delivery(bus_case)
    first.acknowledgement.ack.side_effect = OSError("ack lost")
    await consumer.handle(first)
    await consumer.handle(delivery(bus_case))
    enforcer.apply.assert_awaited_once()
    consumer.store.capacity = 1
    new = delivery(bus_case, {**bus_case.command, "command_id": str(uuid4())})
    await consumer.handle(new)
    new.acknowledgement.ack.assert_not_awaited()
    assert consumer.counters["pending_failure"] == 2


def test_store_capacity_retention_and_outcome_dead_letter_recovery(bus_case):
    store = bus_case.store
    store.capacity = 1
    store.begin(bus_case.command, bus_case.now)
    with pytest.raises(StoreFull):
        store.begin({**bus_case.command, "command_id": str(uuid4())}, bus_case.now)
    store.maintain(bus_case.now + 6)
    for row in store.pending(bus_case.now + 6):
        for _ in range(10):
            store.failed_publish(row["id"], bus_case.now)
    assert store.inspect()["dead_outcomes"] == 2
    assert store.begin(bus_case.command, bus_case.now + 7) == "duplicate"
    assert store.inspect()["pending_outcomes"] == 2
    store.maintain(bus_case.now + 1000)
    assert store.inspect()["inbox"] == 1  # unpublished outcomes retained
    for row in store.pending(bus_case.now + 1000):
        store.published(row["id"])
    store.maintain(bus_case.now + 1000)
    assert store.inspect()["inbox"] == 0


async def test_outcome_publish_failure_retries_without_reapplying(bus_case):
    consumer, enforcer = handler(bus_case)
    await consumer.handle(delivery(bus_case))
    runtime = CommandRuntime(bus_case.config)
    runtime.transport = SimpleNamespace(publish=AsyncMock(side_effect=OSError("offline")))
    await runtime.relay()
    assert runtime.store.inspect()["pending_outcomes"] == 2
    enforcer.apply.assert_awaited_once()
    await runtime.authority.close()


@pytest.mark.parametrize(
    "body",
    [
        {"authorized": True},
        {"authorized": 1},
        {"authorized": "true"},
        {"authorized": True, "unexpected": 1},
        {},
    ],
)
async def test_command_authority_requires_exact_response(bus_case, body):
    authority = HTTPCommandAuthority(bus_case.config)
    await authority.client.aclose()
    authority.client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    )
    assert await authority.check(bus_case.command) is (
        type(body.get("authorized")) is bool and body == {"authorized": True}
    )
    await authority.close()


def test_rotation_and_no_sdp_ice_payload(bus_case):
    key = Ed25519PrivateKey.generate()
    verifier = Verifier(
        {**bus_case.config.verifier.keys, "rotated": TrustedKey("django-control", key.public_key())}
    )
    assert (
        verifier.verify(
            Signer("django-control", "rotated", key).sign(bus_case.command, "command"), "command"
        )[1]
        == bus_case.command
    )
    bad = copy.deepcopy(bus_case.command)
    bad["data"]["sdp"] = "private sdp"
    with pytest.raises(ValueError):
        bus_case.signer.sign(bad, "command")


@pytest.mark.parametrize(
    "invalid", [None, "non_tls", "producer", "peer", "token", "memory", "extra", "oversized"]
)
def test_configuration_is_explicit_and_fails_closed(bus_case, tmp_path, invalid):
    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    private.write_bytes(
        bus_case.config.signer.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public.write_bytes(
        bus_case.signer.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    url, token = tmp_path / "redis-url", tmp_path / "token"
    url.write_text("redis://127.0.0.1:6379/15")
    token.write_text("x" * 32)
    data = {
        "redis_url_file": str(url),
        "producer_id": "owner-1",
        "signing_key_id": "owner-key",
        "private_key_file": str(private),
        "trusted_keys": {
            "django-key": {"producer_id": "django-control", "public_key_file": str(public)}
        },
        "inbox_path": str(tmp_path / "db.sqlite3"),
        "authority_url": "https://synq.example/authority/",
        "authority_token_file": str(token),
    }
    if invalid == "non_tls":
        url.write_text("redis://secret-password@remote.invalid:6379/0")
    if invalid == "producer":
        data["producer_id"] = "django-control"
    if invalid == "peer":
        data["trusted_keys"]["django-key"]["producer_id"] = "another-gateway"
    if invalid == "token":
        token.write_text("short")
    if invalid == "memory":
        data["inbox_path"] = ":memory:"
    if invalid == "extra":
        data["allow_insecure"] = True
    config_path = tmp_path / "bus.json"
    config_path.write_text(json.dumps(data) if invalid != "oversized" else " " * 33000)
    if invalid is None:
        result = BusConfig.read(str(config_path), gateway=True)
        assert result.signer.producer == "owner-1"
        assert (
            result.verifier.verify(bus_case.signer.sign(bus_case.command, "command"), "command")[0]
            == "django-control"
        )
    else:
        with pytest.raises(BusError) as error:
            BusConfig.read(str(config_path), gateway=True)
        assert "secret-password" not in str(error.value)


async def test_consumer_task_startup_failure_is_not_healthy():
    from jrtc_signalling.bus_transport import Streams

    transport = Streams("redis://127.0.0.1:6379/15", "test")

    async def broken():
        raise OSError("private broker details")

    task = asyncio.create_task(broken())
    await asyncio.gather(task, return_exceptions=True)
    transport.consumers.append(SimpleNamespace(_task=task))
    assert not await transport.healthy()
