"""Cryptographic ticket verification and bounded trusted Django authority access."""

import asyncio
import base64
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from .config import ConfigurationError
from .contracts import ContractError, decode_message, validate_message


class AuthError(Exception):
    """Never contains untrusted input, credentials or transport exceptions."""

    def __init__(self, code: str = "UNAUTHENTICATED") -> None:
        self.code = code
        super().__init__(code)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError


def strict_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)


@dataclass(frozen=True, repr=False)
class AuthConfig:
    issuer: str
    public_keys: dict[str, RSAPublicKey]
    origins: frozenset[str]
    authority_url: str
    authority_token: str
    max_connections: int = 256
    auth_timeout: float = 5.0
    authority_timeout: float = 2.0
    cache_seconds: float = 0.5

    def __post_init__(self) -> None:
        try:
            if not 1 <= len(self.issuer) <= 256 or not 1 <= len(self.public_keys) <= 8:
                raise ValueError
            for kid, key in self.public_keys.items():
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", kid):
                    raise ValueError
                if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
                    raise ValueError
            if not 1 <= len(self.origins) <= 32:
                raise ValueError
            for origin in self.origins:
                parsed = urlsplit(origin)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                    or parsed.netloc.endswith(":")
                ):
                    raise ValueError
                _ = parsed.port
            endpoint = urlsplit(self.authority_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError
            _ = endpoint.port
            if not 32 <= len(self.authority_token) <= 4096 or not self.authority_token.isascii():
                raise ValueError
            if any(ord(c) <= 32 or ord(c) == 127 for c in self.authority_token):
                raise ValueError
            if type(self.max_connections) is not int or not 1 <= self.max_connections <= 4096:
                raise ValueError
            for value, maximum in (
                (self.auth_timeout, 10),
                (self.authority_timeout, 5),
                (self.cache_seconds, 1),
            ):
                if type(value) not in (float, int) or not 0 < value <= maximum:
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError("Invalid authentication configuration.") from None

    @classmethod
    def read(cls, path: str) -> "AuthConfig":
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError
            value = strict_json(raw)
            required = {"issuer", "public_keys", "origins", "authority_url", "authority_token_file"}
            if not isinstance(value, dict) or set(value) != required:
                raise ValueError
            if not isinstance(value["public_keys"], dict) or not isinstance(value["origins"], list):
                raise ValueError
            keys: dict[str, RSAPublicKey] = {}
            for kid, pem in value["public_keys"].items():
                key = serialization.load_pem_public_key(pem.encode("ascii"))
                if not isinstance(key, RSAPublicKey):
                    raise ValueError
                keys[kid] = key
            with Path(value["authority_token_file"]).open("rb") as stream:
                token = stream.read(4098).decode("ascii").strip()
            return cls(
                value["issuer"], keys, frozenset(value["origins"]), value["authority_url"], token
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise ConfigurationError("Invalid authentication configuration file.") from None


class TicketVerifier:
    def __init__(self, config: AuthConfig, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock

    def verify(self, token: str) -> dict[str, Any]:
        try:
            if not isinstance(token, str) or len(token) > 8192:
                raise ValueError
            parts = token.split(".")
            if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
                raise ValueError
            decoded = [
                base64.b64decode(p + "=" * (-len(p) % 4), altchars=b"-_", validate=True)
                for p in parts[:2]
            ]
            header = decode_message(decoded[0], schema="ticket-header")
            claims = decode_message(decoded[1], schema="ticket-claims")
            key = self.config.public_keys[header["kid"]]
            verified = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience="jrtc-signalling",
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "strict_aud": True,
                },
            )
            if verified != claims:
                raise ValueError
            # Strict zero-skew checks are within the protocol's maximum 5-second allowance.
            now = self.clock()
            if now < claims["iat"] or now < claims["nbf"] or now >= claims["exp"]:
                raise AuthError("EXPIRED_TICKET")
            return claims
        except (ValueError, KeyError, TypeError, jwt.PyJWTError, ContractError):
            raise AuthError() from None


@dataclass(frozen=True, repr=False)
class Grant:
    claims: dict[str, Any]
    permissions: dict[str, bool]
    valid_until: int


class Authority(Protocol):
    async def check(self, claims: dict[str, Any], *, operation: str, consume: bool) -> Grant: ...

    async def aclose(self) -> None: ...


class HTTPAuthority:
    def __init__(self, config: AuthConfig, client: httpx2.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx2.AsyncClient(
            timeout=config.authority_timeout,
            follow_redirects=False,
            limits=httpx2.Limits(max_connections=16, max_keepalive_connections=8),
        )

    async def check(self, claims: dict[str, Any], *, operation: str, consume: bool) -> Grant:
        try:
            async with asyncio.timeout(self.config.authority_timeout):
                async with self.client.stream(
                    "POST",
                    self.config.authority_url,
                    headers={"Authorization": "Bearer " + self.config.authority_token},
                    json={"claims": claims, "operation": operation, "consume": consume},
                    follow_redirects=False,
                ) as reply:
                    if reply.status_code != 200:
                        raise AuthError(
                            "FORBIDDEN" if reply.status_code in {401, 403} else "UNAVAILABLE"
                        )
                    body = bytearray()
                    async for part in reply.aiter_bytes(chunk_size=4096):
                        if len(body) + len(part) > 16384:
                            raise ValueError
                        body.extend(part)
                    value = strict_json(bytes(body))
            if not isinstance(value, dict) or set(value) != {
                "claims",
                "permissions",
                "valid_until",
            }:
                raise ValueError
            current = validate_message(value["claims"], schema="ticket-claims")
            permissions = value["permissions"]
            if (
                current != claims
                or not isinstance(permissions, dict)
                or set(permissions) != {"audio", "video", "screen"}
                or any(type(v) is not bool for v in permissions.values())
                or type(value["valid_until"]) is not int
                or value["valid_until"] != claims["exp"]
            ):
                raise ValueError
            if "lobby" in claims["scopes"] and any(permissions.values()):
                raise ValueError
            return Grant(current, permissions, value["valid_until"])
        except (httpx2.HTTPError, TimeoutError, ValueError, TypeError, KeyError, RecursionError):
            raise AuthError("UNAVAILABLE") from None

    async def aclose(self) -> None:
        await self.client.aclose()


class AuthorizationSession:
    """One bounded cached grant; an invalidation racing an RPC cannot be undone."""

    def __init__(
        self,
        authority: Authority,
        claims: dict[str, Any],
        *,
        cache_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.authority = authority
        self.claims = claims
        self.cache_seconds = cache_seconds
        self.clock, self.wall_clock = clock, wall_clock
        self.grant: Grant | None = None
        self.deadline = 0.0
        self.revision = 0
        self.minimum_epoch = claims["authorization_epoch"]
        self.revoked = False

    def invalidate(self, epoch: int) -> None:
        if epoch >= self.minimum_epoch:
            self.minimum_epoch = epoch
            self.revoked = True
            self.revision += 1
            self.grant = None

    async def check(self, operation: str, *, consume: bool = False) -> Grant:
        if self.wall_clock() >= self.claims["exp"]:
            raise AuthError("EXPIRED_TICKET")
        if self.revoked or self.claims["authorization_epoch"] < self.minimum_epoch:
            raise AuthError("STALE_AUTHORIZATION")
        ephemeral = operation in {"media.trickle", "session.ping"} and not consume
        if ephemeral and self.grant is not None and self.clock() < self.deadline:
            return self.grant
        revision = self.revision
        # The cache age includes RPC time, so slow checks cannot extend stale state.
        started = self.clock()
        self.grant = None
        try:
            result = await self.authority.check(self.claims, operation=operation, consume=consume)
        except AuthError:
            raise
        except Exception:
            raise AuthError("UNAVAILABLE") from None
        if self.revision != revision or self.wall_clock() >= result.valid_until:
            raise AuthError("STALE_AUTHORIZATION")
        self.grant, self.deadline = result, started + self.cache_seconds
        return result

    async def authorize(self, operation: str, payload: dict[str, Any]) -> None:
        if operation.startswith("media."):
            if "lobby" in self.claims["scopes"]:
                raise AuthError("FORBIDDEN")
            scope = (
                "media:subscribe" if operation.startswith("media.subscriber.") else "media:publish"
            )
            if (
                operation in {"media.trickle", "media.ice_restart", "media.hangup"}
                and payload.get("handle_type") == "subscriber"
            ):
                scope = "media:subscribe"
            if scope not in self.claims["scopes"]:
                raise AuthError("FORBIDDEN")
        grant = await self.check(operation)
        if operation in {"media.publisher.publish", "media.publisher.configure"}:
            for track in payload["tracks"]:
                kind, permission = {
                    "microphone": ("audio", "audio"),
                    "camera": ("video", "video"),
                    "screen_share": ("video", "screen"),
                    "screen_share_audio": ("audio", "screen"),
                }[track["source"]]
                if track["kind"] != kind:
                    raise AuthError("FORBIDDEN")
                if not grant.permissions[permission]:
                    raise AuthError("FORBIDDEN")
