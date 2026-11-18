import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from jrtc.models.response import SuccessResponse
from starlette.datastructures import Headers

from jrtc_signalling.auth import AuthError
from jrtc_signalling.bus import command_route
from jrtc_signalling.multiworker import Elections, Route, worker_routes
from jrtc_signalling.ownership import OwnerLease, OwnershipError, validate_owner_request
from jrtc_signalling.routing import DirectConnection, resolve

ROUTES = {"a": "wss://a.example/ws", "b": "wss://b.example/ws"}


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"a": "ws://a.example/ws"},
        {"a": "wss://user@a.example/ws"},
        {"a": "wss://a.example/ws?room=123"},
        {"wrong/owner": ROUTES["a"]},
        {"a": ROUTES["a"], "b": ROUTES["a"]},
        {"a": "wss://host:invalid/ws"},
    ],
)
def test_direct_routes_must_be_distinct_tls_endpoints(invalid):
    with pytest.raises(ValueError):
        worker_routes(invalid)


def test_relay_does_not_follow_redirects():
    error = OSError("test response")
    assert DirectConnection.process_redirect(None, error) is error


@pytest.mark.parametrize("session_id", [True, -1, 0, 2**63, "123"])
def test_election_requires_positive_internal_session_id(session_id):
    with pytest.raises(ValueError):
        validate_owner_request(
            {
                "issued_at": 1,
                "request_id": str(uuid4()),
                "meeting_id": str(uuid4()),
                "incarnation": str(uuid4()),
                "generation": 0,
                "action": "elect",
                "pool": "a" * 64,
                "session_id": session_id,
            }
        )


async def test_reroute_requires_a_current_local_generation_and_at_most_one_hop():
    runtime = SimpleNamespace(
        owner_id="a",
        incarnation=str(uuid4()),
        stopping=asyncio.Event(),
        retiring=False,
        require=lambda m, gen: None,
    )
    route = Route("b", str(uuid4()), 2)
    elections = SimpleNamespace(
        runtime=runtime, lookup=AsyncMock(return_value=route), routes=ROUTES
    )
    ws = SimpleNamespace(headers=Headers({}))
    assert await resolve(elections, "m", ws) == ROUTES["b"]
    ws.headers = Headers({"x-jrtc-owner-hop": "1"})
    with pytest.raises(AuthError):
        await resolve(elections, "m", ws)
    elections.lookup.return_value = Route("a", str(uuid4()), 1)
    with pytest.raises(AuthError):
        await resolve(elections, "m", ws)
    elections.lookup.return_value = Route("a", runtime.incarnation, 3)
    assert await resolve(elections, "m", ws) is None
    runtime.retiring = True
    with pytest.raises(AuthError):
        await resolve(elections, "m", ws)


@pytest.mark.skipif(not os.getenv("GATEWAY_TEST_REDIS_URL"), reason="isolated Redis required")
async def test_redis_single_flight_expiry_and_delayed_withdrawal():
    url = os.environ["GATEWAY_TEST_REDIS_URL"]
    meeting = str(uuid4())

    def contender(owner):
        session = SimpleNamespace(id=42 if owner == "a" else 43)
        runtime = SimpleNamespace(
            owner_id=owner,
            incarnation=str(uuid4()),
            config=SimpleNamespace(url="ws://127.0.0.1:8188", api_secret="x" * 32),
            bus=SimpleNamespace(redis_url=url),
            authority=SimpleNamespace(inspect=AsyncMock(return_value=(0, None))),
            stopping=asyncio.Event(),
            retiring=False,
            leases={},
            session=lambda m: session,
        )
        runtime.authority.request = AsyncMock(
            return_value=OwnerLease(
                meeting, owner, runtime.incarnation, 1, asyncio.get_running_loop().time() + 29
            )
        )
        return Elections(runtime, ROUTES)

    a, b = contender("a"), contender("b")
    try:
        for _ in range(12):
            leases = await asyncio.gather(a.campaign(meeting), b.campaign(meeting))
            [index] = [idx for idx, lease in enumerate(leases) if lease is not None]
            winner, standby = (a, b) if index == 0 else (b, a)
            lease = leases[index]
            assert (await standby.lookup(meeting)) == winner.route(lease)
            assert await winner.swap(meeting, winner.route(lease).encode(), "")
        # Force expiry of the old route; a delayed withdraw must not delete its successor.
        old = await a.campaign(meeting)
        assert await a.redis.pexpire(a.key(meeting), 0)
        fresh = await b.campaign(meeting)
        assert fresh is not None
        with pytest.raises(OwnershipError):
            await a.renew(meeting, old)
        await a.withdraw({meeting: old})
        assert await a.lookup(meeting) == b.route(fresh)
    finally:
        await a.redis.delete(a.key(meeting))
        await asyncio.gather(a.redis.aclose(), b.redis.aclose())


def test_rolling_processes_with_one_signer_have_distinct_command_streams():
    old, new = str(uuid4()), str(uuid4())
    assert command_route("a", old) != command_route("a", new)
    assert command_route("a", old).startswith(command_route("a") + ".")
    with pytest.raises(ValueError):
        command_route("a", "unknown")


async def test_destroy_success_without_remote_absence_cannot_promote():
    session = SimpleNamespace(id=101, send=AsyncMock(return_value=SuccessResponse(janus="success")))
    with pytest.raises(OwnershipError, match="still reachable"):
        await Elections._clean_previous(None, session, str(uuid4()), 1, 99)
    assert session.send.await_count == 2


async def test_concurrent_endpoint_shutdown_waits_for_the_same_cleanup(auth_config, authority):
    from jrtc_signalling.config import Settings
    from jrtc_signalling.websocket_auth import AuthenticatedEndpoint

    endpoint = AuthenticatedEndpoint(auth_config, Settings(), authority)
    started, finish = asyncio.Event(), asyncio.Event()

    async def close():
        started.set()
        await finish.wait()

    authority.aclose = AsyncMock(side_effect=close)
    first = asyncio.create_task(endpoint.aclose())
    await started.wait()
    second = asyncio.create_task(endpoint.aclose())
    await asyncio.sleep(0)
    assert not second.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    await second
    authority.aclose.assert_awaited_once()
