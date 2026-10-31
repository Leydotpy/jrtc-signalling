"""Explicit standalone configuration, with safe errors and no Django settings."""

import math
import os
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Reports configuration fields without echoing their values."""


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    websocket_path: str = "/ws"
    startup_timeout: float = 10.0
    shutdown_timeout: float = 10.0
    websocket_max_size: int = 128 * 1024
    websocket_max_queue: int = 16
    websocket_ping_interval: float = 25.0
    websocket_ping_timeout: float = 20.0
    log_level: str = "info"
    auth_config_file: str = ""
    bus_config_file: str = ""
    runtime_config_file: str = ""
    tls_cert_file: str = ""
    tls_key_file: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not re.fullmatch(r"[A-Za-z0-9.:-]{1,253}", self.host):
            raise ConfigurationError("Invalid HOST.")
        for field, value, maximum in [
            ("PORT", self.port, 65535),
            ("WS_MAX_SIZE", self.websocket_max_size, 128 * 1024),
            ("WS_MAX_QUEUE", self.websocket_max_queue, 64),
        ]:
            if type(value) is not int or not 1 <= value <= maximum:
                raise ConfigurationError(f"Invalid {field}.")
        if (
            not isinstance(self.websocket_path, str)
            or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", self.websocket_path)
            or self.websocket_path in {"/health", "/health/live", "/health/ready"}
        ):
            raise ConfigurationError("Invalid WS_PATH.")
        for timeout_field, timeout_value in [
            ("STARTUP_TIMEOUT", self.startup_timeout),
            ("SHUTDOWN_TIMEOUT", self.shutdown_timeout),
            ("WS_PING_INTERVAL", self.websocket_ping_interval),
            ("WS_PING_TIMEOUT", self.websocket_ping_timeout),
        ]:
            if (
                isinstance(timeout_value, bool)
                or not isinstance(timeout_value, (int, float))
                or not math.isfinite(timeout_value)
                or not 0 < timeout_value <= 120
            ):
                raise ConfigurationError(f"Invalid {timeout_field}.")
        # Debug/trace websocket logs may contain credentials or frames.
        if self.log_level not in {"info", "warning", "error", "critical"}:
            raise ConfigurationError("Invalid LOG_LEVEL.")
        if not isinstance(self.auth_config_file, str) or len(self.auth_config_file) > 4096:
            raise ConfigurationError("Invalid AUTH_CONFIG_FILE.")
        if not isinstance(self.bus_config_file, str) or len(self.bus_config_file) > 4096:
            raise ConfigurationError("Invalid BUS_CONFIG_FILE.")
        if not isinstance(self.runtime_config_file, str) or len(self.runtime_config_file) > 4096:
            raise ConfigurationError("Invalid RUNTIME_CONFIG_FILE.")
        if self.runtime_config_file and not self.bus_config_file:
            raise ConfigurationError("Runtime ownership requires BUS_CONFIG_FILE.")
        if any(
            not isinstance(path, str) or len(path) > 4096
            for path in (self.tls_cert_file, self.tls_key_file)
        ):
            raise ConfigurationError("Invalid TLS configuration.")
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ConfigurationError("Both TLS_CERT_FILE and TLS_KEY_FILE are required.")
        if self.tls_cert_file:
            try:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.tls_cert_file, self.tls_key_file, password="")
            except (OSError, ValueError):
                raise ConfigurationError("Invalid TLS certificate or key.") from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        prefix = "JRTC_SIGNALLING_"
        known = {
            "HOST",
            "PORT",
            "WS_PATH",
            "STARTUP_TIMEOUT",
            "SHUTDOWN_TIMEOUT",
            "WS_MAX_SIZE",
            "WS_MAX_QUEUE",
            "WS_PING_INTERVAL",
            "WS_PING_TIMEOUT",
            "LOG_LEVEL",
            "AUTH_CONFIG_FILE",
            "BUS_CONFIG_FILE",
            "RUNTIME_CONFIG_FILE",
            "TLS_CERT_FILE",
            "TLS_KEY_FILE",
        }
        if any(key.startswith(prefix) and key.removeprefix(prefix) not in known for key in env):
            raise ConfigurationError("Unknown JRTC_SIGNALLING configuration field.")
        try:
            return cls(
                host=env.get(prefix + "HOST", "127.0.0.1"),
                port=int(env.get(prefix + "PORT", "8765")),
                websocket_path=env.get(prefix + "WS_PATH", "/ws"),
                startup_timeout=float(env.get(prefix + "STARTUP_TIMEOUT", "10")),
                shutdown_timeout=float(env.get(prefix + "SHUTDOWN_TIMEOUT", "10")),
                websocket_max_size=int(env.get(prefix + "WS_MAX_SIZE", "131072")),
                websocket_max_queue=int(env.get(prefix + "WS_MAX_QUEUE", "16")),
                websocket_ping_interval=float(env.get(prefix + "WS_PING_INTERVAL", "25")),
                websocket_ping_timeout=float(env.get(prefix + "WS_PING_TIMEOUT", "20")),
                log_level=env.get(prefix + "LOG_LEVEL", "info"),
                auth_config_file=env.get(prefix + "AUTH_CONFIG_FILE", ""),
                bus_config_file=env.get(prefix + "BUS_CONFIG_FILE", ""),
                runtime_config_file=env.get(prefix + "RUNTIME_CONFIG_FILE", ""),
                tls_cert_file=env.get(prefix + "TLS_CERT_FILE", ""),
                tls_key_file=env.get(prefix + "TLS_KEY_FILE", ""),
            )
        except (ValueError, TypeError):
            raise ConfigurationError(
                "Invalid JRTC_SIGNALLING configuration; check field names and limits."
            ) from None
