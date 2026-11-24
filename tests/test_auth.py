import asyncio
import base64
import json
from dataclasses import replace

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from jrtc_signalling.auth import (
    AuthConfig,
    AuthError,
    AuthorizationSession,
    HTTPAuthority,
    TicketVerifier,
)
from jrtc_signalling.config import ConfigurationError


def test_real_signature_and_pinned_trust(auth_config, claims, sign):
    assert TicketVerifier(auth_config).verify(sign(claims)) == claims


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://evil.example"},
        {"aud": "other"},
        {"aud": ["jrtc-signalling"]},
        {"exp": 1},
        {"exp": 9999999999},
        {"nbf": 9999999999},
        {"generation": 0},
        {"generation": True},
        {"authorization_epoch": -1},
        {"sub": ""},
        {"scopes": ["lobby", "media:publish"]},
        {"scopes": ["admin"]},
        {"extra": "forged"},
    ],
)
def test_signed_but_invalid_claims_reject(auth_config, claims, sign, change):
    with pytest.raises(AuthError):
        TicketVerifier(auth_config).verify(sign({**claims, **change}))


@pytest.mark.parametrize(
    "key",
    [
        "iss",
        "aud",
        "sub",
        "jti",
        "iat",
        "nbf",
        "exp",
        "meeting_id",
        "participant_id",
        "participant_session_id",
        "generation",
        "authorization_epoch",
        "scopes",
        "grant_id",
    ],
)
def test_missing_required_claim_rejects(auth_config, claims, sign, key):
    claims.pop(key)
    with pytest.raises(AuthError):
        TicketVerifier(auth_config).verify(sign(claims))


@pytest.mark.parametrize(
    "header",
    [
        {"kid": "unknown"},
        {"kid": "key-1", "typ": "other"},
        {"kid": "key-1", "jku": "https://evil.example/key"},
        {"kid": "key-1", "crit": ["exp"]},
        {"kid": "../../key"},
    ],
)
def test_header_is_strict_and_cannot_select_network_keys(auth_config, claims, sign, header):
    with pytest.raises(AuthError):
        TicketVerifier(auth_config).verify(sign(claims, headers=header))


def test_algorithm_confusion_signature_tampering_and_wrong_key(auth_config, claims, sign):
    tokens = [
        jwt.encode(
            claims, "untrusted-symmetric-secret" * 3, algorithm="HS256", headers={"kid": "key-1"}
        ),
        jwt.encode(claims, None, algorithm="none", headers={"kid": "key-1"}),
        jwt.encode(
            claims,
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
            algorithm="RS256",
            headers={"kid": "key-1"},
        ),
    ]
    good = sign(claims)
    header, payload, signature = good.split(".")
    tokens.append(
        ".".join([header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]])
    )
    for token in tokens:
        with pytest.raises(AuthError):
            TicketVerifier(auth_config).verify(token)


@pytest.mark.parametrize("raw", ["", "x", "." * 3, "a.a.%", "private-token-" * 1000, "\ud800"])
def test_malformed_tokens_are_input_free(auth_config, raw):
    with pytest.raises(AuthError) as error:
        TicketVerifier(auth_config).verify(raw)
    assert str(error.value) == "UNAUTHENTICATED"


def test_duplicate_claims_reject_even_with_valid_signature(auth_config, claims, private_key):
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    header = b64(b'{"alg":"RS256","typ":"JWT","kid":"key-1"}')
    payload = b64((json.dumps(claims)[:-1] + ',"generation":9}').encode())
    body = header + b"." + payload
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(AuthError):
        TicketVerifier(auth_config).verify((body + b"." + b64(signature)).decode())


def test_time_boundaries_and_rotation_overlap(auth_config, claims, private_key, sign):
    token = sign(claims)
    for now in [claims["iat"] - 0.1, claims["exp"], claims["exp"] + 1]:
        with pytest.raises(AuthError):
            TicketVerifier(auth_config, clock=lambda now=now: now).verify(token)
    rotated = replace(
        auth_config, public_keys={**auth_config.public_keys, "key-2": private_key.public_key()}
    )
    assert TicketVerifier(rotated).verify(sign(claims, headers={"kid": "key-2"})) == claims
    with pytest.raises(AuthError):
        TicketVerifier(replace(rotated, public_keys={"key-2": private_key.public_key()})).verify(
            token
        )


@pytest.mark.parametrize(
    "change",
    [
        {"origins": frozenset()},
        {"origins": frozenset({"https://meet.example/"})},
        {"origins": frozenset({"*"})},
        {"origins": frozenset({"https://user@meet.example"})},
        {"authority_url": "http://synq.example"},
        {"authority_url": "https://synq.example?key=x"},
        {"authority_token": "short"},
        {"authority_token": "x" * 33 + "\n"},
        {"cache_seconds": 2},
        {"cache_seconds": float("nan")},
        {"max_connections": True},
    ],
)
def test_insecure_config_is_rejected(auth_config, change):
    with pytest.raises(ConfigurationError):
        replace(auth_config, **change)


def test_config_file_public_only_bounded_and_input_free(tmp_path, auth_config, private_key):
    token_file = tmp_path / "token"
    token_file.write_text(auth_config.authority_token)
    path = tmp_path / "auth.json"
    data = {
        "issuer": auth_config.issuer,
        "origins": list(auth_config.origins),
        "authority_url": auth_config.authority_url,
        "authority_token_file": str(token_file),
        "public_keys": {
            "key-1": private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        },
    }
    path.write_text(json.dumps(data))
    assert AuthConfig.read(str(path)).origins == auth_config.origins
    data["public_keys"]["key-1"] = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError) as error:
        AuthConfig.read(str(path))
    assert "PRIVATE" not in str(error.value)
    assert "synthetic" not in repr(auth_config)


async def test_authority_rpc_is_authenticated_and_response_bound(auth_config, claims):
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + auth_config.authority_token
        assert json.loads(request.content) == {
            "claims": claims,
            "operation": "session.hello",
            "consume": True,
        }
        return httpx2.Response(
            200,
            json={
                "claims": claims,
                "permissions": {"audio": True, "video": True, "screen": False},
                "valid_until": claims["exp"],
            },
        )

    authority = HTTPAuthority(
        auth_config, httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    assert (await authority.check(claims, operation="session.hello", consume=True)).claims == claims
    await authority.aclose()


@pytest.mark.parametrize(
    "mode",
    [
        "redirect",
        "denied",
        "server_error",
        "oversize",
        "invalid",
        "wrong_claims",
        "permissions",
        "expired",
    ],
)
async def test_authority_uncertainty_fails_closed(auth_config, claims, mode):
    def handler(request):
        if mode == "redirect":
            return httpx2.Response(302, headers={"Location": "https://evil.example"})
        if mode in {"denied", "server_error"}:
            return httpx2.Response(403 if mode == "denied" else 500)
        if mode in {"oversize", "invalid"}:
            return httpx2.Response(
                200, content=b"private-response" * 2000 if mode == "oversize" else b"not json"
            )
        body = {
            "claims": dict(claims),
            "permissions": {"audio": True, "video": True, "screen": False},
            "valid_until": claims["exp"],
        }
        if mode == "wrong_claims":
            body["claims"]["generation"] += 1
        if mode == "permissions":
            body["permissions"]["audio"] = 1
        if mode == "expired":
            body["valid_until"] -= 1
        return httpx2.Response(200, json=body)

    authority = HTTPAuthority(
        auth_config, httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    with pytest.raises(AuthError) as error:
        await authority.check(claims, operation="session.hello", consume=True)
    assert "private" not in str(error.value)
    await authority.aclose()


async def test_authority_timeout_has_finite_budget(auth_config, claims):
    async def handler(request):
        await asyncio.Event().wait()

    config = replace(auth_config, authority_timeout=0.01)
    authority = HTTPAuthority(config, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    with pytest.raises(AuthError):
        await authority.check(claims, operation="session.hello", consume=True)
    await authority.aclose()


async def test_ice_cache_is_bounded_and_privileged_commands_refresh(authority, claims):
    now = [0.0]
    session = AuthorizationSession(authority, claims, clock=lambda: now[0])
    await session.check("session.hello", consume=True)
    for _ in range(20):
        await session.check("media.trickle")
    assert len(authority.calls) == 1
    await session.check("media.publisher.join")
    assert len(authority.calls) == 2
    now[0] = 0.5
    await session.check("media.trickle")
    assert len(authority.calls) == 3
    authority.active = False
    with pytest.raises(AuthError):
        await session.check("media.publisher.join")
    assert session.grant is None
    with pytest.raises(AuthError):
        await session.check("media.trickle")


async def test_revoke_racing_rpc_cannot_restore_cache(authority, claims):
    started, release = asyncio.Event(), asyncio.Event()
    original = authority.check

    async def blocked(*args, **kwargs):
        result = await original(*args, **kwargs)
        started.set()
        await release.wait()
        return result

    authority.check = blocked
    session = AuthorizationSession(authority, claims)
    task = asyncio.create_task(session.check("session.hello", consume=True))
    await started.wait()
    session.invalidate(claims["authorization_epoch"] + 1)
    release.set()
    with pytest.raises(AuthError):
        await task
    assert session.grant is None
    with pytest.raises(AuthError):
        await session.check("media.trickle")


@pytest.mark.parametrize(
    "source,kind",
    [
        ("microphone", "audio"),
        ("camera", "video"),
        ("screen_share", "video"),
        ("screen_share_audio", "audio"),
    ],
)
async def test_track_grants_checked_independently(authority, claims, source, kind):
    authority.permissions = dict.fromkeys(authority.permissions, False)
    session = AuthorizationSession(authority, claims)
    with pytest.raises(AuthError):
        await session.authorize(
            "media.publisher.publish", {"tracks": [{"source": source, "kind": kind}]}
        )


async def test_subscriber_scope_and_lobby_cannot_be_confused(authority, claims):
    claims["scopes"] = ["media:subscribe", "session:resume"]
    session = AuthorizationSession(authority, claims)
    await session.authorize("media.subscriber.join", {})
    with pytest.raises(AuthError):
        await session.authorize("media.publisher.join", {})
    claims["scopes"] = ["lobby", "session:resume"]
    with pytest.raises(AuthError):
        await session.authorize("media.subscriber.join", {})
