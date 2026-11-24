import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jrtc_signalling.auth import AuthConfig, AuthError, Grant
from jrtc_signalling.bus import BusConfig, Signer, TrustedKey, Verifier
from jrtc_signalling.command_store import CommandStore


@pytest.fixture(scope="session")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def auth_config(private_key):
    return AuthConfig(
        issuer="https://synq.example",
        public_keys={"key-1": private_key.public_key()},
        origins=frozenset({"https://meet.example"}),
        authority_url="https://synq.example/api/v1/meetings/internal/gateway/authority/",
        authority_token="synthetic-service-token-" + "x" * 32,
    )


@pytest.fixture
def claims():
    now = int(time.time())
    return {
        "iss": "https://synq.example",
        "aud": "jrtc-signalling",
        "sub": "profile-1",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "meeting_id": "meeting-1",
        "participant_id": "participant-1",
        "participant_session_id": "connection-1",
        "grant_id": "grant-1",
        "generation": 1,
        "authorization_epoch": 3,
        "scopes": ["media:publish", "media:subscribe", "session:resume"],
    }


@pytest.fixture
def sign(private_key):
    def encode(claims, **kwargs):
        headers = kwargs.pop("headers", {"kid": "key-1", "typ": "JWT"})
        return jwt.encode(claims, private_key, algorithm="RS256", headers=headers, **kwargs)

    return encode


class FakeAuthority:
    def __init__(self):
        self.calls = []
        self.used = set()
        self.active = True
        self.closed = False
        self.permissions = {"audio": True, "video": True, "screen": False}

    async def check(self, claims, *, operation, consume):
        self.calls.append((operation, consume))
        if not self.active or (consume and claims["jti"] in self.used):
            raise AuthError("FORBIDDEN")
        if consume:
            self.used.add(claims["jti"])
        permissions = dict(self.permissions)
        if "lobby" in claims["scopes"]:
            permissions = dict.fromkeys(permissions, False)
        return Grant(dict(claims), permissions, claims["exp"])

    async def aclose(self):
        self.closed = True


@pytest.fixture
def authority():
    return FakeAuthority()


@pytest.fixture
def bus_case(tmp_path):
    django, worker = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    signer = Signer("django-control", "django-key", django)
    config = BusConfig(
        "redis://127.0.0.1:6379/15",
        Signer("owner-1", "owner-key", worker),
        Verifier({"django-key": TrustedKey("django-control", django.public_key())}),
        str(tmp_path / "inbox.sqlite3"),
        "https://synq.example/internal/authorize/",
        "x" * 32,
    )
    now = time.time()
    command = {
        "schema_version": 1,
        "command_id": str(uuid4()),
        "type": "participant.disconnect",
        "meeting_id": str(uuid4()),
        "participant_id": str(uuid4()),
        "participant_session_id": str(uuid4()),
        "authorization_epoch": 2,
        "target_owner": "owner-1",
        "expected_owner_generation": 3,
        "issued_at": datetime.fromtimestamp(now, UTC).isoformat(),
        "expires_at": datetime.fromtimestamp(now + 60, UTC).isoformat(),
        "actor_id": str(uuid4()),
        "trace_id": str(uuid4()),
        "data": {"reason_code": "revoked"},
    }
    store = CommandStore(config.inbox_path)
    store.initialize()
    return SimpleNamespace(signer=signer, config=config, command=command, now=now, store=store)
