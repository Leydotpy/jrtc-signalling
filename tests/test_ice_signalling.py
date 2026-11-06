import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from media_helpers import publish_payload, sdp
from starlette.websockets import WebSocketDisconnect
from test_ice import candidate
from test_media_signalling import rehearsal as rehearsal

CLIENT_CASES = json.loads((Path(__file__).parent / "fixtures/client-ice.json").read_text())["cases"]


async def ready(r, role):
    browser = await r.browser()
    published = (await browser.request("media.publisher.publish", publish_payload()))["result"]
    if role == "publisher":
        return browser, published
    browser = await r.browser()
    joined = (await browser.request("media.subscriber.join"))["result"]
    assert (
        await browser.request(
            "media.subscriber.start",
            {
                "answer": {"type": "answer", "sdp": sdp("recvonly")},
                "negotiation_id": joined["negotiation_id"],
            },
        )
    )["ok"]
    return browser, joined


def payload(role, result, count=1, completed=False, ice="sg08"):
    return {
        "handle_type": role,
        "negotiation_id": result["negotiation_id"],
        "candidates": [candidate(i + 1, ice=ice) for i in range(count)],
        "completed": completed,
    }


def sent(r):
    return [q for q in r.fake.requests if q["janus"] == "trickle"]


@pytest.mark.parametrize("role", ["publisher", "subscriber"])
@pytest.mark.parametrize("case", CLIENT_CASES, ids=lambda c: c["name"])
async def test_actual_client_batcher_fixtures_across_gateway_sdk_boundary(rehearsal, role, case):
    r = rehearsal
    browser, result = await ready(r, role)
    expected = []
    for batch in case["batches"]:
        request = payload(role, result) | batch
        assert (await browser.request("media.trickle", request))["ok"]
        if batch["candidates"]:
            expected.append(
                {
                    "candidates": [
                        {k: v for k, v in c.items() if k != "usernameFragment"}
                        for c in batch["candidates"]
                    ]
                }
            )
        if batch["completed"]:
            expected.append({"candidate": {"completed": True}})
    assert [
        {k: v for k, v in q.items() if k in {"candidate", "candidates"}} for q in sent(r)
    ] == expected


@pytest.mark.parametrize("role", ["publisher", "subscriber"])
@pytest.mark.parametrize(
    "count,completed", [(1, False), (16, False), (3, True), (0, True), (32, True)]
)
async def test_batch_matrix_preserves_one_array_then_completion(rehearsal, role, count, completed):
    r = rehearsal
    browser, result = await ready(r, role)
    request = payload(role, result, count, completed)
    response = await browser.request("media.trickle", request)
    assert response["result"]["action"] == "trickle"
    wire = sent(r)
    assert len(wire) == bool(count) + completed
    if count:
        assert "candidate" not in wire[0]
        assert wire[0]["candidates"] == [
            {k: v for k, v in c.items() if k != "usernameFragment"} for c in request["candidates"]
        ]
        assert type(wire[0]["handle_id"]) is int and wire[0]["handle_id"] > 2**53
    if completed:
        assert wire[-1]["candidate"] == {"completed": True}
        assert (await browser.request("media.trickle", payload(role, result, 0, True)))["result"][
            "action"
        ] == "noop"
        assert len(sent(r)) == len(wire)
        assert (await browser.request("media.trickle", payload(role, result)))["error"][
            "code"
        ] == "STALE_NEGOTIATION"


async def test_pipelined_batches_wait_for_previous_ack_and_exact_replay_is_noop(rehearsal):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    r.fake.block = "trickle"
    first = browser.message("media.trickle", payload("publisher", result, 16))
    final = browser.message("media.trickle", payload("publisher", result, 3, True))
    await browser.send(first)
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    await browser.send(first)
    await browser.send(final)
    assert len(sent(r)) == 1 and "candidate" not in sent(r)[0]
    r.fake.release.set()
    first_reply = await browser.reply(first["id"])
    assert (await browser.reply(final["id"]))["ok"]
    assert [len(q.get("candidates", [])) for q in sent(r)] == [16, 3, 0]
    await browser.send(first)
    assert await browser.reply(first["id"]) == first_reply
    assert len(sent(r)) == 3


@pytest.mark.parametrize("mode", ["error", "timeout", "cancel", "revoke", "lease"])
async def test_failed_or_uncertain_batch_never_completes_or_retries(rehearsal, mode):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    r.fake.block = "trickle"
    request = browser.message("media.trickle", payload("publisher", result, 3, True))
    await browser.send(request)
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    peer = r.hub.peers[(browser.claims["meeting_id"], browser.claims["participant_id"])]
    if mode == "cancel":
        await browser.close()
    else:
        if mode == "error":
            r.fake.trickle_error = True
        elif mode == "revoke":
            peer.authorization.invalidate(browser.claims["authorization_epoch"] + 1)
        elif mode == "lease":
            lease = r.runtime.leases[peer.meeting]
            r.runtime.leases[peer.meeting] = replace(lease, deadline=0)
        if mode != "timeout":
            r.fake.release.set()
        if mode in {"error", "timeout", "lease"}:
            assert (await browser.reply(request["id"]))["error"]["code"] == "OUTCOME_UNKNOWN"
        with pytest.raises(WebSocketDisconnect):
            while True:
                await browser.receive()
        if mode == "lease":
            r.runtime.leases[peer.meeting] = lease
    r.fake.release.set()
    assert len(sent(r)) == 1 and "candidate" not in sent(r)[0]
    assert not r.runtime.registry.bindings


@pytest.mark.parametrize(
    "change",
    [
        {"negotiation_id": str(uuid4())},
        {"candidates": [candidate(), candidate(2, ice="old1")]},
        {"candidates": [candidate(), candidate(2, mid="other")]},
    ],
)
async def test_entire_batch_is_validated_before_any_janus_mutation(rehearsal, change):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    response = await browser.request("media.trickle", payload("publisher", result) | change)
    assert response["error"]["code"] == "STALE_NEGOTIATION"
    assert not sent(r)


@pytest.mark.parametrize(
    "change",
    [
        {"candidates": []},
        {"candidates": [candidate()] * 33},
        {"candidates": [{"candidate": "private-malformed-input", "sdpMid": "0"}]},
        {
            "candidates": [
                candidate() | {"candidate": candidate()["candidate"] + " extension " + "x" * 3000}
            ]
            * 32
        },
        {"completed": "true"},
    ],
)
async def test_bad_wire_payload_rejects_before_authority_or_sdk(rehearsal, change):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    before = len(r.authority.calls)
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.request("media.trickle", payload("publisher", result) | change)
    assert error.value.code == 1008
    assert len(r.authority.calls) == before
    assert not sent(r)


async def test_legacy_single_candidate_is_one_janus_array(rehearsal):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    request = payload("publisher", result)
    request["candidate"] = request.pop("candidates")[0]
    request.pop("completed")
    assert (await browser.request("media.trickle", request))["ok"]
    assert len(sent(r)[0]["candidates"]) == 1


async def test_failed_completion_is_uncertain_and_is_never_acknowledged_as_success(rehearsal):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    r.fake.trickle_error = "completion"
    response = await browser.request("media.trickle", payload("publisher", result, 3, True))
    assert response["error"]["code"] == "OUTCOME_UNKNOWN"
    with pytest.raises(WebSocketDisconnect):
        while True:
            await browser.receive()
    assert [len(q.get("candidates", [])) for q in sent(r)] == [3, 0]
    assert not r.runtime.registry.bindings


async def test_out_of_order_ice_frame_rejects_without_forwarding(rehearsal):
    r = rehearsal
    browser, result = await ready(r, "publisher")
    request = browser.message("media.trickle", payload("publisher", result, 3, True))
    request["sequence"] += 1
    await browser.send(request)
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1008
    assert not sent(r)


@pytest.mark.parametrize("role", ["publisher", "subscriber"])
async def test_restart_reserves_new_negotiation_and_binds_new_browser_credentials(rehearsal, role):
    r = rehearsal
    browser, old = await ready(r, role)
    assert (await browser.request("media.trickle", payload(role, old, 3, True)))["ok"]
    restarted = (await browser.request("media.ice_restart", {"handle_type": role}))["result"]
    assert restarted["negotiation_id"] != old["negotiation_id"]
    assert (await browser.request("media.trickle", payload(role, old)))["error"][
        "code"
    ] == "STALE_NEGOTIATION"
    assert (await browser.request("media.trickle", payload(role, restarted)))["error"][
        "code"
    ] == "STALE_NEGOTIATION"
    if role == "publisher":
        assert restarted["jsep"] is None
        negotiate = publish_payload(negotiation=restarted["negotiation_id"])
        operation, field = "media.publisher.configure", "offer"
    else:
        assert restarted["jsep"]["type"] == "offer"
        assert any(q.get("body", {}).get("restart") is True for q in r.fake.requests)
        negotiate = {
            "answer": {"type": "answer", "sdp": sdp("recvonly")},
            "negotiation_id": restarted["negotiation_id"],
        }
        operation, field = "media.subscriber.start", "answer"
    assert (await browser.request(operation, negotiate))["error"]["code"] == "STALE_NEGOTIATION"
    negotiate[field]["sdp"] = sdp("sendonly" if role == "publisher" else "recvonly", ice="sg09")
    assert (await browser.request(operation, negotiate))["ok"]
    assert (await browser.request("media.trickle", payload(role, restarted, ice="sg08")))["error"][
        "code"
    ] == "STALE_NEGOTIATION"
    assert (await browser.request("media.trickle", payload(role, restarted, 3, True, "sg09")))["ok"]
    assert [len(q.get("candidates", [])) for q in sent(r)] == [3, 0, 3, 0]


async def test_reconnect_never_adopts_previous_handle_or_negotiation(rehearsal):
    r = rehearsal
    browser, old = await ready(r, "publisher")
    old_handles = set(r.runtime.registry.bindings)
    identity = browser.claims["participant_id"]
    await browser.close()
    new = await r.browser(participant_id=identity)
    assert (await new.request("media.trickle", payload("publisher", old)))["error"][
        "code"
    ] == "STALE_NEGOTIATION"
    current = (await new.request("media.publisher.publish", publish_payload()))["result"]
    assert not old_handles & set(r.runtime.registry.bindings)
    assert (await new.request("media.trickle", payload("publisher", old)))["error"][
        "code"
    ] == "STALE_NEGOTIATION"
    assert (await new.request("media.trickle", payload("publisher", current)))["ok"]


async def test_delayed_old_publisher_answer_cannot_be_associated_with_restart(rehearsal):
    r = rehearsal
    browser, _ = await ready(r, "publisher")
    old_event = copy.deepcopy(
        next(e for e in reversed(r.fake.events) if e.get("jsep", {}).get("type") == "answer")
    )
    restarted = (await browser.request("media.ice_restart", {"handle_type": "publisher"}))["result"]
    offer = publish_payload(negotiation=restarted["negotiation_id"])
    offer["offer"]["sdp"] = sdp(ice="sg09")
    assert (await browser.request("media.publisher.configure", offer))["ok"]
    # Exact old correlated delivery is suppressed even after the new answer.
    await r.fake.send(old_event)
    assert (await browser.request("session.ping"))["ok"]
    assert not any(e["op"] == "media.jsep" for e in browser.notifications)
    old_event["transaction"] = "late-unsolicited-old-generation"
    await r.fake.send(old_event)
    with pytest.raises(WebSocketDisconnect):
        while True:
            assert (await browser.receive())["op"] != "media.jsep"


async def test_unsolicited_restart_offer_is_processed_once_and_retired_offer_rejects(rehearsal):
    r = rehearsal
    browser, old = await ready(r, "subscriber")
    previous = copy.deepcopy(
        next(e for e in reversed(r.fake.events) if e.get("jsep", {}).get("type") == "offer")
    )
    unsolicited = copy.deepcopy(previous)
    unsolicited["transaction"] = "unsolicited-restart"
    unsolicited["jsep"]["sdp"] = sdp(ice="srv2")
    await r.fake.send(unsolicited)
    while True:
        event = await browser.receive()
        if event["op"] == "media.jsep":
            break
    current = event["payload"]
    assert current["negotiation_id"] != old["negotiation_id"]
    assert (
        await browser.request(
            "media.subscriber.start",
            {
                "negotiation_id": current["negotiation_id"],
                "answer": {"type": "answer", "sdp": sdp("recvonly", ice="cli2")},
            },
        )
    )["ok"]
    await r.fake.send(unsolicited)
    assert (await browser.request("session.ping"))["ok"]
    assert not any(e["op"] == "media.jsep" for e in browser.notifications)
    previous["transaction"] = "delayed-retired-offer"
    await r.fake.send(previous)
    with pytest.raises(WebSocketDisconnect):
        while True:
            assert (await browser.receive())["op"] != "media.jsep"
