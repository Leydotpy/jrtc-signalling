import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from media_helpers import Browser, MockJanus, publish_payload, sdp
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from jrtc_signalling.app import create_app
from jrtc_signalling.config import Settings
from jrtc_signalling.contracts import validate_message
from jrtc_signalling.janus_runtime import JanusConfig, JanusRuntime
from jrtc_signalling.ownership import OwnerLease
from jrtc_signalling.runtime import ComponentStatus, DependencyHealth


@pytest.fixture
async def rehearsal(bus_case, auth_config, authority, claims, sign):
    fake = MockJanus()
    meeting = str(uuid4())
    other_meeting = str(uuid4())
    browsers = []
    async with serve(fake.handler, "127.0.0.1", 0, subprotocols=["janus-protocol"]) as server:
        port = server.sockets[0].getsockname()[1]
        runtime = JanusRuntime(
            bus_case.config,
            JanusConfig(
                f"ws://127.0.0.1:{port}",
                "x" * 32,
                "https://synq.example/owners/",
                (meeting, other_meeting),
            ),
        )

        async def lease(meeting, action, generation=0):
            return OwnerLease(
                meeting,
                runtime.owner_id,
                runtime.incarnation,
                1,
                asyncio.get_running_loop().time() + 30,
            )

        runtime.authority.request = AsyncMock(side_effect=lease)
        runtime.control = SimpleNamespace(
            start=AsyncMock(),
            drain=AsyncMock(),
            aclose=AsyncMock(),
            health=lambda: DependencyHealth(broka=ComponentStatus.READY),
        )
        app = create_app(
            Settings(media_rehearsal=True, shutdown_timeout=30),
            runtime_factory=lambda: runtime,
            auth_config=auth_config,
            authority_factory=lambda config: authority,
        )
        async with app.router.lifespan_context(app):

            async def browser(**changes):
                bound = {
                    **claims,
                    "meeting_id": meeting,
                    "participant_id": str(uuid4()),
                    "participant_session_id": str(uuid4()),
                    "jti": str(uuid4()),
                    **changes,
                }
                client = Browser(app, bound, sign)
                browsers.append(client)
                ready = await client.open()
                validate_message(ready, schema="response")
                return client

            try:
                yield SimpleNamespace(
                    app=app,
                    runtime=runtime,
                    fake=fake,
                    browser=browser,
                    authority=authority,
                    hub=app.state.auth_endpoint.media,
                    metrics=app.state.auth_endpoint.metrics,
                    other_meeting=other_meeting,
                )
            finally:
                for client in browsers:
                    await client.close()
        assert not fake.rooms
        assert not runtime.registry.bindings


async def test_publisher_calls_typed_sdk_and_preserves_handle_after_unpublish(rehearsal):
    r = rehearsal
    browser = await r.browser()
    assert (await browser.request("media.publisher.join"))["result"]["action"] == "join"
    first = (await browser.request("media.publisher.publish", publish_payload()))["result"]
    assert first["action"] == "publish"
    assert first["jsep"]["type"] == "answer"
    assert int(first["streams"][0]["janus_feed_id"]) > 2**53
    peer = next(iter(r.hub.peers.values()))
    token = peer.handles["publisher"].ref.token
    configured = await browser.request("media.publisher.configure", publish_payload())
    assert configured["result"]["action"] == "configure"
    assert (await browser.request("media.publisher.unpublish"))["result"]["action"] == "unpublish"
    assert (await browser.request("media.publisher.unpublish"))["result"]["action"] == "noop"
    assert peer.handles["publisher"].ref.token == token
    await browser.request("media.publisher.publish", publish_payload())
    assert r.metrics.operations["media.publisher.publish"].count == 2
    assert r.metrics.operations["media.publisher.publish"].maximum_seconds > 0


async def test_subscriber_sources_are_gateway_owned_and_answer_bound(rehearsal):
    r = rehearsal
    publisher, subscriber = await r.browser(), await r.browser(scopes=["media:subscribe"])
    published = (await publisher.request("media.publisher.publish", publish_payload()))["result"]
    assert published["action"] == "join_and_configure"
    joined = (await subscriber.request("media.subscriber.join"))["result"]
    assert joined["jsep"]["type"] == "offer"
    assert joined["selected_streams"][0]["feed"] == published["streams"][0]["janus_feed_id"]
    assert joined["selected_streams"][0]["crossrefid"] is None
    wrong = await subscriber.request(
        "media.subscriber.start",
        {"answer": {"type": "answer", "sdp": sdp("recvonly")}, "negotiation_id": "old"},
    )
    assert wrong["error"]["code"] == "STALE_NEGOTIATION"
    started = await subscriber.request(
        "media.subscriber.start",
        {
            "answer": {"type": "answer", "sdp": sdp("recvonly")},
            "negotiation_id": joined["negotiation_id"],
        },
    )
    assert started["result"]["action"] == "start"
    assert (await subscriber.request("media.subscriber.sync"))["result"]["action"] == "noop"
    second = await r.browser()
    await second.request("media.publisher.publish", publish_payload())
    update = (await subscriber.request("media.subscriber.update"))["result"]
    assert update["action"] == "update"
    assert len(update["selected_streams"]) == 2
    answer = update["jsep"]["sdp"].replace("a=sendonly", "a=recvonly")
    await subscriber.request(
        "media.subscriber.start",
        {"answer": {"type": "answer", "sdp": answer}, "negotiation_id": update["negotiation_id"]},
    )
    assert (await subscriber.request("media.subscriber.detach"))["result"][
        "lifecycle_state"
    ] == "detached"


async def test_correlated_sdp_once_and_unsolicited_sdp_forwarded(rehearsal):
    r = rehearsal
    publisher, subscriber = await r.browser(), await r.browser()
    await publisher.request("media.publisher.publish", publish_payload())
    joined = (await subscriber.request("media.subscriber.join"))["result"]
    response = next(e for e in reversed(r.fake.events) if e.get("jsep", {}).get("type") == "offer")
    await subscriber.request(
        "media.subscriber.start",
        {
            "answer": {"type": "answer", "sdp": sdp("recvonly")},
            "negotiation_id": joined["negotiation_id"],
        },
    )
    await r.fake.send(response)
    # The event worker drains before the ordered ping response is collected.
    await subscriber.request("session.ping")
    assert not any(e["op"] == "media.jsep" for e in subscriber.notifications)
    unsolicited = {**response, "transaction": "unsolicited-new-offer"}
    await r.fake.send(unsolicited)
    while True:
        event = await subscriber.receive()
        if event["op"] == "media.jsep":
            break
    validate_message(event, schema="notification")
    assert event["payload"]["negotiation_id"] != joined["negotiation_id"]
    assert event["payload"]["transaction"] == "unsolicited-new-offer"


async def test_exact_replay_and_pending_duplicate_do_not_repeat_mutation(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    request = browser.message("media.publisher.publish", publish_payload())
    await browser.send(request)
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    await browser.send(request)
    # Reader can accept pipelined frames while the single mutation is outstanding.
    ping = browser.message("session.ping")
    await browser.send(ping)
    r.fake.release.set()
    result = await browser.reply(request["id"])
    assert (await browser.reply(ping["id"]))["ok"]
    await browser.send(request)
    assert await browser.reply(request["id"]) == result
    assert sum(x.get("body", {}).get("request") == "joinandconfigure" for x in r.fake.requests) == 1
    assert r.metrics.coalesced == 1
    assert r.metrics.replayed == 1


@pytest.mark.parametrize(
    "bad", ["undeclared_audio", "kind_mismatch", "screen_denied", "extra_data"]
)
async def test_unauthorized_sdp_never_reaches_janus(rehearsal, bad):
    r = rehearsal
    browser = await r.browser()
    payload = publish_payload()
    if bad == "undeclared_audio":
        payload["tracks"] = []
    elif bad == "kind_mismatch":
        payload["offer"]["sdp"] = sdp(kinds=("video",))
    elif bad == "screen_denied":
        payload = publish_payload(kind="video", source="screen_share")
    else:
        payload["offer"]["sdp"] += (
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\na=mid:data\r\n"
        )
    before = len(r.fake.requests)
    if bad == "extra_data":
        assert (await browser.request("media.publisher.publish", payload))["error"][
            "code"
        ] == "INVALID_MESSAGE"
    else:
        with pytest.raises(WebSocketDisconnect) as error:
            await browser.request("media.publisher.publish", payload)
        assert error.value.code == 1008
    assert len(r.fake.requests) == before


async def test_disconnect_cancels_outstanding_sdk_operation_and_disposes_handle(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    await browser.close()
    assert not r.hub.peers
    assert not r.runtime.registry.bindings
    r.fake.release.set()


async def test_bounded_request_queue_closes_overloaded_client(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    for _ in range(10):
        await browser.send(browser.message("session.ping"))
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1013
    r.fake.release.set()


async def test_duplicate_id_with_changed_payload_fails_closed(rehearsal):
    browser = await rehearsal.browser()
    request = browser.message("session.ping")
    await browser.send(request)
    await browser.reply(request["id"])
    changed = copy.deepcopy(request)
    changed["op"] = "media.publisher.join"
    await browser.send(changed)
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.receive()
    assert error.value.code == 1008


async def test_feed_waits_for_webrtc_ready_and_is_isolated_by_meeting(rehearsal):
    r = rehearsal
    publisher, subscriber = await r.browser(), await r.browser()
    foreign = await r.browser(meeting_id=r.other_meeting)
    r.fake.webrtc_up = False
    await publisher.request("media.publisher.publish", publish_payload())
    assert (await subscriber.request("media.subscriber.join"))["result"]["action"] == "noop"
    handle = next(iter(r.hub.peers.values())).handles["publisher"]
    await r.fake.send(
        {"janus": "webrtcup", "session_id": 777, "sender": handle.ref.janus_handle_id}
    )
    while True:
        event = await publisher.receive()
        if event["payload"]["kind"] == "webrtcup":
            break
    assert (await subscriber.request("media.subscriber.join"))["result"]["action"] == "join"
    assert (await foreign.request("media.subscriber.join"))["result"]["action"] == "noop"


@pytest.mark.parametrize("bad", ["malformed", "extra_mid", "wrong_direction"])
async def test_invalid_janus_answer_is_uncertain_and_handle_is_disposed(rehearsal, bad):
    r = rehearsal
    browser = await r.browser()

    def corrupt(result):
        if "jsep" in result:
            result["jsep"]["sdp"] = {
                "malformed": "not-sdp",
                "extra_mid": sdp("recvonly", kinds=("audio", "video")),
                "wrong_direction": sdp("sendonly"),
            }[bad]
        return result

    r.fake.mutate_response = corrupt
    result = await browser.request("media.publisher.publish", publish_payload())
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert not result["error"]["retryable"]
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1012
    assert not r.runtime.registry.bindings


async def test_unsolicited_offer_cannot_add_unselected_media(rehearsal):
    r = rehearsal
    publisher, subscriber = await r.browser(), await r.browser()
    await publisher.request("media.publisher.publish", publish_payload())
    joined = (await subscriber.request("media.subscriber.join"))["result"]
    response = next(e for e in reversed(r.fake.events) if e.get("jsep", {}).get("type") == "offer")
    await subscriber.request(
        "media.subscriber.start",
        {
            "answer": {"type": "answer", "sdp": sdp("recvonly")},
            "negotiation_id": joined["negotiation_id"],
        },
    )
    await r.fake.send(
        {
            **response,
            "transaction": "new",
            "jsep": {"type": "offer", "sdp": sdp(kinds=("audio", "video"))},
        }
    )
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            event = await subscriber.receive()
            assert event["op"] != "media.jsep"
    assert error.value.code == 1012


@pytest.mark.parametrize("scopes", [["lobby"], ["media:subscribe"]])
async def test_missing_publish_scope_cannot_allocate_janus_resources(rehearsal, scopes):
    r = rehearsal
    browser = await r.browser(scopes=scopes)
    before = len(r.fake.requests)
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.request("media.publisher.publish", publish_payload())
    assert error.value.code == 1008
    assert len(r.fake.requests) == before


async def test_second_connection_cannot_adopt_active_participant_handles(rehearsal):
    r = rehearsal
    first = await r.browser()
    await first.request("media.publisher.join")
    with pytest.raises(WebSocketDisconnect) as error:
        await r.browser(participant_id=first.claims["participant_id"])
    assert error.value.code == 1008
    assert len(r.runtime.registry.bindings) == 1
    assert (await first.request("session.ping"))["ok"]


async def test_current_authority_denial_blocks_mutation_and_disposes_media(rehearsal):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    r.authority.active = False
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.request("media.publisher.configure", publish_payload())
    assert error.value.code == 1008
    assert not any(x.get("body", {}).get("request") == "configure" for x in r.fake.requests)
    assert not r.runtime.registry.bindings


async def test_revocation_racing_janus_reply_cannot_restore_publish(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    peer = next(iter(r.hub.peers.values()))
    peer.authorization.invalidate(browser.claims["authorization_epoch"] + 1)
    r.fake.release.set()
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            assert "result" not in await browser.receive()
    assert error.value.code == 1008
    assert not r.runtime.registry.bindings


async def test_slow_authority_check_does_not_misclassify_inflight_grant_as_revocation(rehearsal):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.publish", publish_payload())
    original = r.authority.check

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)  # Cross a guard tick while the grant is being checked.
        return await original(*args, **kwargs)

    r.authority.check = slow
    assert (await browser.request("media.publisher.configure", publish_payload()))["ok"]


async def test_sdk_timeout_is_never_retried_and_returns_uncertain_outcome(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    result = await browser.request("media.publisher.publish", publish_payload())
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1012
    assert sum(x.get("body", {}).get("request") == "joinandconfigure" for x in r.fake.requests) == 1
    r.fake.release.set()


async def test_request_bytes_are_bounded_before_count_limit(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    for _ in range(5):
        payload = publish_payload()
        payload["offer"]["sdp"] += "a=x:" + "x" * 60000 + "\r\n"
        await browser.send(browser.message("media.publisher.publish", payload))
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1013
    r.fake.release.set()


async def test_slow_websocket_writer_has_bounded_deadline(rehearsal):
    r = rehearsal
    browser = await r.browser()
    browser.stall_sends = True
    await browser.send(browser.message("session.ping"))
    await asyncio.wait_for(browser.send_blocked.wait(), 1)
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.receive()
    assert error.value.code == 1013
    assert not r.hub.peers


async def test_unsolicited_event_queue_overflow_closes_without_blocking_transport(rehearsal):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.join")
    peer = next(iter(r.hub.peers.values()))
    handle = peer.handles["publisher"]
    # The actual SDK callback is deliberately synchronous and cannot wait for I/O.
    for _ in range(33):
        peer._event(handle, SimpleNamespace(janus="media"))
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1013


async def test_shutdown_closes_and_cancels_pending_media(rehearsal):
    r = rehearsal
    browser = await r.browser()
    r.fake.block = "joinandconfigure"
    await browser.send(browser.message("media.publisher.publish", publish_payload()))
    await asyncio.wait_for(r.fake.entered.wait(), 2)
    await r.app.state.auth_endpoint.aclose()
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.receive()
    assert error.value.code == 1012
    assert not r.runtime.registry.bindings
    assert not r.hub.peers
    assert r.metrics.active == 0
    r.fake.release.set()
    # Endpoint cancellation is expected during ASGI shutdown.
    assert browser.task.cancelled()
    browser.task = None


async def test_global_dispatch_concurrency_is_bounded(rehearsal):
    r = rehearsal
    browsers = [await r.browser() for _ in range(17)]
    original = r.authority.check
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def blocked(*args, **kwargs):
        nonlocal count
        if kwargs["operation"] == "media.publisher.join":
            count += 1
            if count == 16:
                entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    r.authority.check = blocked
    tasks = [asyncio.create_task(browser.request("media.publisher.join")) for browser in browsers]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert count == r.metrics.active == r.metrics.peak_active == 16
        release.set()
        results = await asyncio.gather(*tasks)
        assert all(result["ok"] for result in results)
        assert r.metrics.active == 0
        assert r.metrics.peak_active == 16
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_evicted_request_cannot_reexecute_with_old_sequence(rehearsal, monkeypatch):
    from jrtc_signalling.signalling import SocketSession

    monkeypatch.setattr(SocketSession, "CACHE_LIMIT", 2)
    r = rehearsal
    browser = await r.browser()
    request = browser.message("media.publisher.publish", publish_payload())
    await browser.send(request)
    await browser.reply(request["id"])
    await browser.request("session.ping")
    await browser.request("session.ping")
    await browser.send(request)
    with pytest.raises(WebSocketDisconnect) as error:
        while True:
            await browser.receive()
    assert error.value.code == 1008
    assert sum(x.get("body", {}).get("request") == "joinandconfigure" for x in r.fake.requests) == 1


async def test_subscriber_request_cannot_supply_foreign_feed(rehearsal):
    r = rehearsal
    browser = await r.browser()
    before = len(r.fake.requests)
    with pytest.raises(WebSocketDisconnect) as error:
        await browser.request("media.subscriber.join", {"feed": "12345"})
    assert error.value.code == 1008
    assert len(r.fake.requests) == before


async def test_refresh_extends_an_already_waiting_reader_deadline(rehearsal):
    browser = await rehearsal.browser(exp=int(time.time()) + 2)
    fresh = {**browser.claims, "exp": int(time.time()) + 60, "jti": str(uuid4())}
    assert (await browser.request("session.refresh", {"ticket": browser.sign(fresh)}))["ok"]
    await asyncio.sleep(2.1)
    assert (await browser.request("session.ping"))["ok"]


async def test_cancelled_room_cleanup_retains_ownership_fence(rehearsal):
    r = rehearsal
    browser = await r.browser()
    await browser.request("media.publisher.join")
    await browser.close()
    original = r.runtime.management
    r.runtime.management = AsyncMock(side_effect=asyncio.CancelledError)
    try:
        with pytest.raises(asyncio.CancelledError):
            await r.hub.aclose()
        assert r.runtime.registry.cleanup_failed
    finally:
        # Test fixture now performs confirmed cleanup, so its lease may release.
        r.runtime.management = original
        await r.hub.aclose()
        r.runtime.registry.cleanup_failed = False
