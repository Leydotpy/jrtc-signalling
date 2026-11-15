"""Adversarial moderation through the real gateway, SDK and Janus wire protocol."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from media_helpers import Browser, publish_payload
from starlette.websockets import WebSocketDisconnect
from test_commands import delivery
from test_ice_signalling import payload as ice_payload
from test_media_signalling import rehearsal as rehearsal

from jrtc_signalling.auth import AuthError
from jrtc_signalling.commands import CommandConsumer
from jrtc_signalling.contracts import validate_message
from jrtc_signalling.ownership import OwnershipError


def command(case, browser, kind="participant.disconnect", **changes):
    claims = browser.claims
    body = {
        **case.command,
        "type": kind,
        "meeting_id": claims["meeting_id"],
        "participant_id": claims["participant_id"],
        "participant_session_id": claims["participant_session_id"],
        "authorization_epoch": claims["authorization_epoch"] + 1,
        "expected_owner_generation": 1,
        **changes,
    }
    if kind == "meeting.terminate":
        body.update(participant_id=None, participant_session_id=None)
    elif kind in {"participant.admit", "participant.permissions.update"}:
        body["data"] = {
            "grant_id": str(uuid4()),
            "scopes": ["media:subscribe"],
            "permissions": {
                "can_publish_audio": False,
                "can_publish_video": False,
                "can_share_screen": False,
                "can_subscribe": True,
            },
        }
    return validate_message(body, schema="command")


def consumer(case, runtime, authorized=True):
    return CommandConsumer(
        case.config,
        case.store,
        SimpleNamespace(check=AsyncMock(return_value=authorized)),
        runtime,
    )


def outcomes(case):
    return [json.loads(row["body"]) for row in case.store.pending(case.now + 100)]


async def disconnected(browser):
    with pytest.raises(WebSocketDisconnect):
        while True:
            await browser.receive()
    await asyncio.wait_for(browser.task, 5)


@pytest.mark.parametrize("phase", ["publish", "trickle"])
async def test_kick_cancels_inflight_and_queued_media_before_applied(rehearsal, bus_case, phase):
    r = rehearsal
    browser = await r.browser()
    if phase == "trickle":
        result = (await browser.request("media.publisher.publish", publish_payload()))["result"]
        r.fake.block = "trickle"
        request = browser.message("media.trickle", ice_payload("publisher", result, 3, True))
    else:
        r.fake.block = "joinandconfigure"
        request = browser.message("media.publisher.publish", publish_payload())
    await browser.send(request)
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    handles = {b.ref.janus_handle_id for b in r.runtime.registry.bindings.values()}
    assert handles
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    wire = delivery(bus_case, command(bus_case, browser))
    handler = consumer(bus_case, r.runtime)
    await handler.handle(wire)
    await disconnected(browser)
    assert handles.isdisjoint(r.fake.handles)
    assert not r.runtime.registry.bindings
    assert not r.runtime.registry.uncertain
    assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "applied"]
    wire.acknowledgement.ack.assert_awaited_once()
    before = len(r.fake.requests)
    await handler.handle(delivery(bus_case, command(bus_case, browser)))
    assert len(r.fake.requests) == before
    assert handler.counters["duplicate"] == 1
    if phase == "trickle":
        assert sum(q["janus"] == "trickle" for q in r.fake.requests) == 1
        assert not any(q.get("candidate", {}).get("completed") for q in r.fake.requests)
    else:
        assert (
            sum(q.get("body", {}).get("request") == "joinandconfigure" for q in r.fake.requests)
            == 1
        )
    r.fake.release.set()


@pytest.mark.parametrize("kind", ["participant.deny", "participant.disconnect"])
@pytest.mark.parametrize("epoch_offset", [0, 1])
async def test_deny_before_admit_fences_hello_resume_and_stale_admit(
    rehearsal, bus_case, kind, epoch_offset
):
    r = rehearsal
    lobby = await r.browser(scopes=["lobby"])
    epoch = lobby.claims["authorization_epoch"] + epoch_offset
    revoke = command(bus_case, lobby, kind, authorization_epoch=epoch)
    assert await r.runtime.apply(revoke) == ("applied", "normal")
    await disconnected(lobby)
    admit = command(bus_case, lobby, "participant.admit", authorization_epoch=epoch)
    assert await r.runtime.apply(admit) == ("failed", "revoked")
    for operation in ("session.hello", "session.resume"):
        stale = Browser(
            r.app, {**lobby.claims, "jti": str(uuid4()), "scopes": ["session:resume"]}, lobby.sign
        )
        original = stale.request

        async def request(op, payload, target=operation, original=original):
            return await original(target, payload)

        stale.request = request
        try:
            with pytest.raises(WebSocketDisconnect):
                await stale.open()
        finally:
            await stale.close()
    assert not r.runtime.registry.bindings


async def test_permission_epoch_change_closes_host_media_without_granting_from_command(
    rehearsal, bus_case
):
    r = rehearsal
    host, other = await r.browser(), await r.browser()
    await host.request("media.publisher.publish", publish_payload())
    change = command(bus_case, host, "participant.permissions.update")
    assert await r.runtime.apply(change) == ("applied", "normal")
    await disconnected(host)
    assert (await other.request("session.ping"))["ok"]
    with pytest.raises(WebSocketDisconnect):
        await r.browser(participant_id=host.claims["participant_id"])
    # Only a new authority-validated epoch can open a connection, never the bus
    # command alone. The test authority deliberately still accepts stale JWTs.
    newer = await r.browser(participant_id=host.claims["participant_id"], authorization_epoch=5)
    assert await r.runtime.apply(change) == ("failed", "revoked")
    assert (await newer.request("session.ping"))["ok"]


async def test_terminate_is_meeting_scoped_and_confirms_room_absence(rehearsal, bus_case):
    r = rehearsal
    publisher, subscriber = await r.browser(), await r.browser()
    unaffected = await r.browser(meeting_id=r.other_meeting)
    await publisher.request("media.publisher.publish", publish_payload())
    await subscriber.request("media.subscriber.join")
    await unaffected.request("media.publisher.publish", publish_payload())
    room = r.hub.rooms[publisher.claims["meeting_id"]].id
    assert await r.runtime.apply(command(bus_case, publisher, "meeting.terminate")) == (
        "applied",
        "normal",
    )
    await disconnected(publisher)
    await disconnected(subscriber)
    assert room not in r.fake.rooms
    assert (await unaffected.request("session.ping"))["ok"]
    # Django also emits per-participant disconnects when ending a meeting.
    assert await r.runtime.apply(command(bus_case, subscriber)) == ("applied", "normal")
    assert await r.runtime.apply(command(bus_case, subscriber, "participant.admit")) == (
        "failed",
        "revoked",
    )
    assert all(b.ref.meeting_id == r.other_meeting for b in r.runtime.registry.bindings.values())
    with pytest.raises(WebSocketDisconnect):
        await r.browser(authorization_epoch=100)


@pytest.mark.parametrize("operation", ["session.hello", "session.refresh"])
async def test_revocation_cannot_be_undone_by_inflight_authority_reply(
    rehearsal, bus_case, operation
):
    r = rehearsal
    first = await r.browser()
    entered, release = asyncio.Event(), asyncio.Event()
    check = r.authority.check

    async def delayed(claims, **kwargs):
        result = await check(claims, **kwargs)
        if kwargs["operation"] == operation:
            entered.set()
            await release.wait()
        return result

    r.authority.check = delayed
    if operation == "session.hello":
        await first.close()
        stale = Browser(r.app, {**first.claims, "jti": str(uuid4())}, first.sign)
        task = asyncio.create_task(stale.open())
    else:
        stale = first
        fresh = {**first.claims, "jti": str(uuid4())}
        task = asyncio.create_task(first.request("session.refresh", {"ticket": first.sign(fresh)}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await r.runtime.apply(command(bus_case, first)) == ("applied", "normal")
        release.set()
        with pytest.raises(WebSocketDisconnect):
            await task
        assert not r.hub.peers
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await stale.close()


@pytest.mark.parametrize("mode", ["revoked", "authority_down", "permission_removed"])
async def test_idle_publisher_is_removed_without_broker_or_client_cooperation(rehearsal, mode):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    handles = {b.ref.janus_handle_id for b in r.runtime.registry.bindings.values()}
    if mode == "revoked":
        r.authority.active = False
    elif mode == "authority_down":
        r.authority.check = AsyncMock(side_effect=AuthError("UNAVAILABLE"))
    else:
        r.authority.permissions["audio"] = False
    await asyncio.wait_for(disconnected(browser), 4)
    assert handles.isdisjoint(r.fake.handles)
    assert not r.runtime.registry.uncertain


async def test_unconfirmed_detach_is_failed_retained_and_eventually_reconciled(rehearsal, bus_case):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    handles = {b.ref.janus_handle_id for b in r.runtime.registry.bindings.values()}
    respond = r.fake.respond
    reject = True

    async def respond_with_failure(request):
        if reject and request["janus"] == "detach":
            await r.fake.send(
                {
                    "janus": "error",
                    "transaction": request["transaction"],
                    "session_id": 777,
                    "error": {"code": 403, "reason": "synthetic"},
                }
            )
        else:
            await respond(request)

    r.fake.respond = respond_with_failure
    handler = consumer(bus_case, r.runtime)
    wire = command(bus_case, browser)
    try:
        await handler.handle(delivery(bus_case, wire))
        await disconnected(browser)
        assert handles.issubset(r.fake.handles)
        assert r.runtime.registry.uncertain
        assert r.runtime.registry.cleanup_failed
        assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]
        with pytest.raises(WebSocketDisconnect):
            await r.browser(participant_id=browser.claims["participant_id"], authorization_epoch=9)
    finally:
        reject = False
        async with asyncio.timeout(4):
            while r.runtime.registry.uncertain:  # noqa: ASYNC110 - background reconciliation
                await asyncio.sleep(0.02)
    assert handles.isdisjoint(r.fake.handles)
    assert not r.runtime.registry.cleanup_failed
    # Reconciliation does not rewrite the command's historical failure as success.
    before = len(r.fake.requests)
    await handler.handle(delivery(bus_case, wire))
    assert len(r.fake.requests) == before
    assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]


async def test_command_timeout_leaves_revocation_and_cleanup_running(
    rehearsal, bus_case, monkeypatch
):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    handles = {b.ref.janus_handle_id for b in r.runtime.registry.bindings.values()}
    release = asyncio.Event()
    dispose = r.runtime.registry._dispose

    async def delayed(binding):
        await release.wait()
        await dispose(binding)

    monkeypatch.setattr(r.runtime.registry, "_dispose", delayed)
    try:
        await consumer(bus_case, r.runtime).handle(delivery(bus_case, command(bus_case, browser)))
        assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "timed_out"]
        assert handles.issubset(r.fake.handles)
        assert next(iter(r.app.state.auth_endpoint.sessions)).authorization.revoked
    finally:
        release.set()
        await disconnected(browser)
    assert handles.isdisjoint(r.fake.handles)


@pytest.mark.parametrize("fault", ["stale_owner", "revoked_actor", "gateway_down"])
async def test_invalid_command_cannot_disconnect_current_participant(rehearsal, bus_case, fault):
    r = rehearsal
    browser = await r.browser()
    wire = command(bus_case, browser)
    handler = consumer(bus_case, r.runtime, authorized=fault != "revoked_actor")
    if fault == "stale_owner":
        wire["expected_owner_generation"] = 2
    if fault == "gateway_down":
        handler.enforcer = SimpleNamespace(apply=AsyncMock(return_value=("failed", "owner_lost")))
    await handler.handle(delivery(bus_case, wire))
    assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]
    assert (await browser.request("session.ping"))["ok"]
    assert not r.app.state.auth_endpoint.moderation.epochs


async def test_lost_lease_cannot_confirm_moderation(rehearsal, bus_case):
    r = rehearsal
    browser = await r.browser()
    meeting = browser.claims["meeting_id"]
    lease = r.runtime.leases[meeting]
    r.runtime.leases[meeting] = replace(lease, deadline=0)
    try:
        assert await r.runtime.apply(command(bus_case, browser)) == ("failed", "owner_lost")
    finally:
        r.runtime.leases[meeting] = lease


async def test_rate_limit_still_installs_revocation_and_closes_media(rehearsal, bus_case):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    moderation = r.app.state.auth_endpoint.moderation
    moderation.rate.take = lambda: False
    await consumer(bus_case, r.runtime).handle(delivery(bus_case, command(bus_case, browser)))
    await disconnected(browser)
    assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]
    assert moderation.counters["rate_limited"] == 1
    assert not r.runtime.registry.bindings
    assert not r.runtime.registry.uncertain


async def test_failed_room_destruction_reconciles_without_a_false_applied_outcome(
    rehearsal, bus_case
):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    room = r.hub.rooms[browser.claims["meeting_id"]].id
    management = r.runtime.management

    async def fail_destroy(meeting, generation, method, *args, **kwargs):
        if method == "destroy":
            raise OSError("synthetic")
        return await management(meeting, generation, method, *args, **kwargs)

    r.runtime.management = fail_destroy
    try:
        await consumer(bus_case, r.runtime).handle(
            delivery(bus_case, command(bus_case, browser, "meeting.terminate"))
        )
        await disconnected(browser)
        assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]
        assert room in r.fake.rooms
        assert r.app.state.auth_endpoint.moderation.pending_rooms
    finally:
        r.runtime.management = management
        async with asyncio.timeout(4):
            while r.app.state.auth_endpoint.moderation.pending_rooms:  # noqa: ASYNC110 - background cleanup
                await asyncio.sleep(0.02)
    assert room not in r.fake.rooms
    assert [e["status"] for e in outcomes(bus_case)] == ["accepted", "failed"]


async def test_admission_is_not_a_media_connection_and_preserves_current_epoch(rehearsal, bus_case):
    r = rehearsal
    browser = await r.browser()
    assert await r.runtime.apply(
        command(
            bus_case,
            browser,
            "participant.admit",
            authorization_epoch=browser.claims["authorization_epoch"],
        )
    ) == ("applied", "normal")
    assert not r.runtime.registry.bindings
    assert (await browser.request("session.ping"))["ok"]


async def test_ended_meeting_does_not_drain_other_owned_meeting_at_renewal(rehearsal, bus_case):
    r = rehearsal
    ended = await r.browser()
    other = await r.browser(meeting_id=r.other_meeting)
    request = r.runtime.authority.request

    async def authority(meeting, action, generation=0):
        if meeting == ended.claims["meeting_id"] and action == "renew":
            raise OwnershipError("Ended meeting cannot renew.")
        return await request(meeting, action, generation)

    r.runtime.authority.request = authority
    assert await r.runtime.apply(command(bus_case, ended, "meeting.terminate")) == (
        "applied",
        "normal",
    )
    await disconnected(ended)
    await asyncio.sleep(5.2)
    assert (await other.request("session.ping"))["ok"]
    assert r.runtime.active
    assert any(call.args[:2] == (r.other_meeting, "renew") for call in request.await_args_list)
