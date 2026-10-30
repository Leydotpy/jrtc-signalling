"""Framework-independent, signed v1 control messages shared with Django.

Tickets and bus credentials are independent. Producer identity comes from the
configured public key, never from broker headers or an unverified payload.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .contracts import ContractError, _no_duplicates, _reject_constant, validate_message

COMMANDS = "synq.signalling.commands.v1"
EVENTS = "synq.signalling.events.v1"
MAX_PACKET = 32 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class BusError(ValueError):
    """Input-free error safe for logs; no packet, URL, key or exception echo."""


def _read(path: str, limit: int = MAX_PACKET) -> bytes:
    with Path(path).open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise BusError("Control configuration exceeds limit.")
    return value


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse(raw: bytes) -> dict[str, Any]:
    try:
        if len(raw) > MAX_PACKET:
            raise ValueError
        value = json.loads(
            raw.decode(), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise BusError("Invalid control message.") from None


def command_route(owner: str) -> str:
    if not ID.fullmatch(owner):
        raise BusError("Invalid control identity.")
    return f"{COMMANDS}.{owner}"


@dataclass(frozen=True, repr=False)
class Signer:
    producer: str
    key_id: str
    key: Ed25519PrivateKey

    def __post_init__(self) -> None:
        if not ID.fullmatch(self.producer) or not ID.fullmatch(self.key_id):
            raise BusError("Invalid control identity.")

    def sign(self, message: dict[str, Any], kind: str) -> bytes:
        if kind not in {"command", "command-outcome"}:
            raise BusError("Unsupported control message.")
        validate_message(message, schema=kind)
        body = {
            "version": 1,
            "producer": self.producer,
            "key_id": self.key_id,
            "kind": kind,
            "body": message,
        }
        signature = base64.b64encode(self.key.sign(canonical(body))).decode()
        packet = canonical({**body, "signature": signature})
        if len(packet) > MAX_PACKET:
            raise BusError("Control message exceeds limit.")
        return packet


@dataclass(frozen=True, repr=False)
class TrustedKey:
    producer: str
    key: Ed25519PublicKey


@dataclass(frozen=True, repr=False)
class Verifier:
    keys: dict[str, TrustedKey]

    def verify(self, raw: bytes, kind: str) -> tuple[str, dict[str, Any]]:
        try:
            packet = parse(raw)
            if set(packet) != {"version", "producer", "key_id", "kind", "body", "signature"}:
                raise ValueError
            if (
                type(packet["version"]) is not int
                or packet["version"] != 1
                or packet["kind"] != kind
            ):
                raise ValueError
            trusted = self.keys[packet["key_id"]]
            if trusted.producer != packet["producer"]:
                raise ValueError
            signature = base64.b64decode(packet.pop("signature"), validate=True)
            trusted.key.verify(signature, canonical(packet))
            body = validate_message(packet["body"], schema=kind)
            return trusted.producer, body
        except (ValueError, KeyError, TypeError, InvalidSignature, ContractError, RecursionError):
            raise BusError("Untrusted control message.") from None


@dataclass(frozen=True, repr=False)
class BusConfig:
    redis_url: str
    signer: Signer
    verifier: Verifier
    inbox_path: str | None = None
    authority_url: str | None = None
    authority_token: str | None = field(default=None, repr=False)

    @classmethod
    def read(cls, path: str, *, gateway: bool) -> "BusConfig":
        try:
            data = parse(_read(path))
            common = {
                "redis_url_file",
                "producer_id",
                "signing_key_id",
                "private_key_file",
                "trusted_keys",
            }
            extra = {"inbox_path", "authority_url", "authority_token_file"} if gateway else set()
            if set(data) != common | extra:
                raise ValueError
            producer = data["producer_id"]
            if (producer == "django-control") == gateway:
                raise ValueError
            private = serialization.load_pem_private_key(
                _read(data["private_key_file"], 4096), password=None
            )
            if not isinstance(private, Ed25519PrivateKey):
                raise ValueError
            keys = {}
            if (
                not isinstance(data["trusted_keys"], dict)
                or not 1 <= len(data["trusted_keys"]) <= 32
            ):
                raise ValueError
            for key_id, entry in data["trusted_keys"].items():
                if not ID.fullmatch(key_id) or set(entry) != {"producer_id", "public_key_file"}:
                    raise ValueError
                peer = entry["producer_id"]
                if not ID.fullmatch(peer) or (peer == "django-control") != gateway:
                    raise ValueError
                public = serialization.load_pem_public_key(_read(entry["public_key_file"], 4096))
                if not isinstance(public, Ed25519PublicKey):
                    raise ValueError
                keys[key_id] = TrustedKey(peer, public)
            url = _read(data["redis_url_file"], 4096).decode().strip()
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"redis", "rediss"}
                or not parsed.hostname
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            if parsed.scheme == "redis" and parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError
            authority, token, inbox = None, None, None
            if gateway:
                authority = data["authority_url"]
                parsed = urlsplit(authority)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError
                token = _read(data["authority_token_file"], 8192).decode().strip()
                inbox = data["inbox_path"]
                if len(token) < 32 or not Path(inbox).is_absolute() or inbox == ":memory:":
                    raise ValueError
            return cls(
                url,
                Signer(producer, data["signing_key_id"], private),
                Verifier(keys),
                inbox,
                authority,
                token,
            )
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            raise BusError("Invalid control-plane configuration.") from None
