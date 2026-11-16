import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from media_helpers import Browser, publish_payload
from starlette.websockets import WebSocketDisconnect
from test_media_signalling import rehearsal as rehearsal

from jrtc_signalling.auth import AuthError, AuthorizationSession
from jrtc_signalling.reconnect import ConnectionHandoffs


async def resume(r, old, sign, **changes):
    client = Browser(
        r.app,
        {**old.claims, "generation": old.claims["generation"] + 1, "jti": str(uuid4()), **changes},
        sign,
    )
    try:
        await client.open("session.resume")
    except BaseException:
        await client.close()
        raise
    return client


async def test_resume_detaches_old_media_before_rebuilding(rehearsal, sign):
    r = rehearsal
    old = await r.browser()
    first = await old.request("media.publisher.publish", publish_payload())
    old_peer = next(iter(r.hub.peers.values()))
    handle = old_peer.handles["publisher"].ref.janus_handle_id
    new = await resume(r, old, sign)
    try:
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                await old.receive()
        assert closed.value.code == 4009
        assert handle not in r.fake.handles
        second = await new.request("media.publisher.publish", publish_payload())
        assert first["result"]["streams"] != second["result"]["streams"]
        # Delayed teardown is idempotent and cannot remove the replacement.
        await old_peer.aclose()
        assert len(r.hub.peers) == 1
        assert (await new.request("session.ping"))["ok"]
    finally:
        await new.close()


async def test_resume_waits_for_confirmation_of_detach(rehearsal, sign, monkeypatch):
    r = rehearsal
    old = await r.browser()
    await old.request("media.publisher.join")
    entered, release = asyncio.Event(), asyncio.Event()
    detach = r.runtime.registry.detach

    async def delayed(ref):
        entered.set()
        await release.wait()
        return await detach(ref)

    monkeypatch.setattr(r.runtime.registry, "detach", delayed)
    pending = asyncio.create_task(resume(r, old, sign))
    await asyncio.wait_for(entered.wait(), 2)
    assert not pending.done()
    assert len(r.runtime.registry.bindings) == 1
    release.set()
    new = await asyncio.wait_for(pending, 2)
    await new.close()


async def test_revoked_authority_cannot_resume(rehearsal, sign):
    r = rehearsal
    old = await r.browser()
    r.authority.active = False
    with pytest.raises(WebSocketDisconnect) as closed:
        await resume(r, old, sign)
    assert closed.value.code == 1008


async def test_delayed_authority_reply_cannot_replace_newer_socket(rehearsal, sign, monkeypatch):
    r = rehearsal
    old = await r.browser()
    entered, release = asyncio.Event(), asyncio.Event()
    check = r.authority.check

    async def delayed(claims, **kwargs):
        result = await check(claims, **kwargs)
        if claims["generation"] == 2 and kwargs["consume"]:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(r.authority, "check", delayed)
    pending = asyncio.create_task(resume(r, old, sign))
    await entered.wait()
    new = await resume(r, old, sign, generation=3)
    try:
        release.set()
        with pytest.raises(WebSocketDisconnect) as stale:
            await pending
        assert stale.value.code == 1008
        assert (await new.request("session.ping"))["ok"]
        assert len(r.hub.peers) == 1
    finally:
        await new.close()


async def test_uncertain_old_handle_blocks_resume_until_confirmed(rehearsal, sign, monkeypatch):
    r = rehearsal
    old = await r.browser()
    await old.request("media.publisher.join")
    confirm = r.runtime.registry._confirm_detached

    async def unavailable(binding):
        raise AuthError("UNAVAILABLE")

    monkeypatch.setattr(r.runtime.registry, "_confirm_detached", unavailable)
    with pytest.raises(WebSocketDisconnect) as blocked:
        await resume(r, old, sign)
    assert blocked.value.code == 1013
    assert r.runtime.registry.uncertain
    assert not r.hub.peers
    monkeypatch.setattr(r.runtime.registry, "_confirm_detached", confirm)
    await r.runtime.registry.retry_cleanup()
    assert not r.runtime.registry.uncertain
    new = await resume(r, old, sign, generation=3)
    await new.close()


async def test_expired_socket_can_only_resume_with_a_fresh_ticket(rehearsal, sign):
    r = rehearsal
    old = await r.browser(exp=int(time.time()) + 2)
    with pytest.raises(WebSocketDisconnect) as closed:
        await old.receive()
    assert closed.value.code == 4001
    with pytest.raises(WebSocketDisconnect) as expired:
        await resume(r, old, sign)
    assert expired.value.code == 4001
    new = await resume(r, old, sign, exp=int(time.time()) + 60)
    assert (await new.request("session.ping"))["ok"]
    await new.close()


def session(claims, authority, generation):
    return SimpleNamespace(
        authorization=AuthorizationSession(authority, {**claims, "generation": generation}),
        endpoint=SimpleNamespace(moderation=None),
        superseded=False,
        revocation=asyncio.Event(),
        finished=asyncio.Event(),
    )


class TestSession:
    __test__ = False

    def __init__(self, claims, authority, generation):
        self.__dict__.update(session(claims, authority, generation).__dict__)


async def test_cancelled_intermediate_handoff_cannot_skip_old_cleanup(claims, authority):
    handoffs = ConnectionHandoffs()
    old, middle, new = [TestSession(claims, authority, i) for i in (1, 2, 3)]
    await handoffs.claim(old)
    middle_task = asyncio.create_task(handoffs.claim(middle))
    await old.revocation.wait()
    new_task = asyncio.create_task(handoffs.claim(new))
    await middle.revocation.wait()
    middle_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await middle_task
    middle.finished.set()
    handoffs.release(middle)
    await asyncio.sleep(0)
    assert not new_task.done()
    old.finished.set()
    handoffs.release(old)
    await new_task
    with pytest.raises(AuthError, match="STALE_GENERATION"):
        await handoffs.claim(TestSession(claims, authority, 2))


async def test_handoff_timeout_and_history_are_bounded(claims, authority):
    handoffs = ConnectionHandoffs()
    handoffs.TIMEOUT = 0.01
    old = TestSession(claims, authority, 1)
    await handoffs.claim(old)
    with pytest.raises(AuthError, match="UNAVAILABLE"):
        await handoffs.claim(TestSession(claims, authority, 2))
    handoffs.LIMIT = 1
    with pytest.raises(AuthError, match="OVERLOADED"):
        await handoffs.claim(TestSession({**claims, "participant_id": "other"}, authority, 1))
