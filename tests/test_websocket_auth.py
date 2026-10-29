import time
import uuid
from dataclasses import replace
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from jrtc_signalling.app import create_app
from jrtc_signalling.config import Settings
from jrtc_signalling.contracts import validate_message
from jrtc_signalling.websocket_auth import PROTOCOL


def envelope(claims, op="session.hello", sequence=1, **payload):
    return {
        "version": 1,
        "id": str(uuid.uuid4()),
        "op": op,
        "meeting_id": claims["meeting_id"],
        "participant_session_id": claims["participant_session_id"],
        "generation": claims["generation"],
        "sequence": sequence,
        "payload": payload,
    }


def connect(client, **kwargs):
    return client.websocket_connect(
        "wss://testserver/ws",
        subprotocols=[PROTOCOL],
        headers={"origin": "https://meet.example"},
        **kwargs,
    )


@pytest.fixture
def app(auth_config, authority):
    return create_app(
        Settings(), auth_config=auth_config, authority_factory=lambda config: authority
    )


def authenticate(ws, claims, sign):
    ws.send_json(envelope(claims, ticket=sign(claims)))
    result = ws.receive_json()
    validate_message(result, schema="response")
    assert result["ok"] is True
    assert result["result"] == {}  # Authentication does not invent an owner lease.


def test_authenticated_control_session_keeps_media_readiness_closed(app, authority, claims, sign):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        ws.send_json(envelope(claims, "session.ping", 2))
        assert ws.receive_json()["ok"]
        ws.send_json(envelope(claims, "media.publisher.join", 3))
        reply = ws.receive_json()
        assert reply["error"]["code"] == "UNAVAILABLE"
        assert client.get("/health/ready").status_code == 503
        assert len(app.state.auth_endpoint.connections) == 1
    assert authority.closed
    assert app.state.auth_endpoint is None


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "https://evil.example",
        "https://meet.example.evil",
        "https://meet.example/",
        "http://meet.example",
    ],
)
def test_origin_must_match_exactly_before_upgrade(app, origin):
    headers = {} if origin is None else {"origin": origin}
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as error,
            client.websocket_connect(
                "wss://testserver/ws", headers=headers, subprotocols=[PROTOCOL]
            ),
        ):
            pytest.fail("unexpected upgrade")
        assert error.value.status_code == 403


@pytest.mark.parametrize(
    "url,protocols",
    [
        ("ws://testserver/ws", [PROTOCOL]),
        ("wss://testserver/ws?ticket=do-not-echo", [PROTOCOL]),
        ("wss://testserver/ws", []),
        ("wss://testserver/ws", [PROTOCOL, "token-do-not-echo"]),
    ],
)
def test_transport_query_and_subprotocol_policy(app, url, protocols):
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDenialResponse) as error,
        client.websocket_connect(
            url, headers={"origin": "https://meet.example"}, subprotocols=protocols
        ),
    ):
        pytest.fail("unexpected upgrade")
    assert "do-not-echo" not in error.value.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("meeting_id", "another"),
        ("participant_session_id", "another"),
        ("generation", 2),
        ("sequence", 2),
    ],
)
def test_first_frame_is_bound_to_ticket(app, authority, claims, sign, field, value):
    message = envelope(claims, ticket=sign(claims))
    message[field] = value
    with TestClient(app) as client, connect(client) as ws:
        ws.send_json(message)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
    assert authority.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("meeting_id", "another"),
        ("participant_session_id", "another"),
        ("generation", 2),
        ("sequence", 1),
    ],
)
def test_every_frame_rechecks_binding_and_sequence(app, claims, sign, field, value):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        message = envelope(claims, "session.ping", 2)
        message[field] = value
        ws.send_json(message)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_replayed_ticket_across_connections_denied(app, claims, sign):
    with TestClient(app) as client, connect(client) as first, connect(client) as second:
        authenticate(first, claims, sign)
        second.send_json(envelope(claims, ticket=sign(claims)))
        with pytest.raises(WebSocketDisconnect):
            second.receive_json()


def test_lobby_cannot_reach_media_authorization(app, authority, claims, sign):
    claims["scopes"] = ["lobby", "session:resume"]
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        ws.send_json(envelope(claims, "media.subscriber.join", 2))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert authority.calls == [("session.hello", True)]


def test_current_revoke_blocks_privileged_transition(app, authority, claims, sign):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        authority.active = False
        ws.send_json(envelope(claims, "media.publisher.join", 2))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_refresh_uses_new_ticket_and_preserves_sequence(app, authority, claims, sign):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        refreshed = {**claims, "jti": str(uuid.uuid4()), "authorization_epoch": 4}
        ws.send_json(envelope(claims, "session.refresh", 2, ticket=sign(refreshed)))
        assert ws.receive_json()["ok"]
        ws.send_json(envelope(refreshed, "session.ping", 3))
        assert ws.receive_json()["ok"]
        ws.send_json(envelope(refreshed, "session.refresh", 4, ticket=sign(refreshed)))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert ("session.refresh", True) in authority.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("sub", "other"),
        ("participant_id", "other"),
        ("grant_id", "other"),
        ("generation", 2),
        ("authorization_epoch", 2),
    ],
)
def test_refresh_cannot_switch_identity_or_regress_epoch(app, claims, sign, field, value):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        fresh = {**claims, "jti": str(uuid.uuid4()), field: value}
        ws.send_json(envelope(claims, "session.refresh", 2, ticket=sign(fresh)))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_missing_hello_times_out_and_releases_capacity(auth_config, authority):
    app = create_app(
        Settings(),
        auth_config=replace(auth_config, auth_timeout=0.02),
        authority_factory=lambda config: authority,
    )
    with TestClient(app) as client, connect(client) as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        assert len(app.state.auth_endpoint.connections) == 0


def test_connection_budget_counts_pending_auth(auth_config, authority):
    app = create_app(
        Settings(),
        auth_config=replace(auth_config, max_connections=1),
        authority_factory=lambda config: authority,
    )
    with TestClient(app) as client, connect(client):
        with pytest.raises(WebSocketDenialResponse) as error, connect(client):
            pytest.fail("capacity exceeded")
        assert error.value.status_code == 503


def test_per_connection_rate_limit_closes_session(app, claims, sign):
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        with patch("jrtc_signalling.websocket_auth.RateLimit.take", return_value=False):
            ws.send_json(envelope(claims, "session.ping", 2))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


@pytest.mark.parametrize("frame", ["malformed", "binary", "oversize", "media_first", "duplicate"])
def test_bad_frames_close_safely_without_credentials_in_logs(app, claims, sign, caplog, frame):
    secret = "sensitive-ticket-must-not-appear"
    with TestClient(app) as client, connect(client) as ws:
        if frame == "binary":
            ws.send_bytes(secret.encode())
        elif frame == "oversize":
            ws.send_text(secret * 10000)
        elif frame == "media_first":
            ws.send_json(envelope(claims, "media.publisher.join"))
        elif frame == "duplicate":
            ws.send_text('{"id":"' + secret + '","id":"duplicate"}')
        else:
            ws.send_json(envelope(claims, ticket=secret))
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
        assert secret not in error.value.reason
    assert secret not in caplog.text


def test_ticket_expires_even_if_client_is_idle(app, claims, sign):
    claims["exp"] = int(time.time()) + 2
    with TestClient(app) as client, connect(client) as ws:
        authenticate(ws, claims, sign)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
