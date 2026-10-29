import json
import logging

import pytest

from jrtc_signalling.config import ConfigurationError, Settings
from jrtc_signalling.logging import SafeJSONFormatter


def test_environment_settings_do_not_require_django_or_services():
    assert Settings.from_env({}) == Settings()
    configured = Settings.from_env(
        {
            "JRTC_SIGNALLING_PORT": "9001",
            "JRTC_SIGNALLING_WS_PATH": "/signal/v1",
            "DJANGO_SETTINGS_MODULE": "unavailable.conf",
        }
    )
    assert configured.port == 9001
    assert configured.websocket_path == "/signal/v1"


@pytest.mark.parametrize(
    "name,value",
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "secret-value"),
        ("HOST", "bad-host\nsecret-value"),
        ("WS_PATH", "ws"),
        ("WS_PATH", "/health"),
        ("WS_PATH", "/health/live"),
        ("WS_PATH", "/health/ready"),
        ("WS_PATH", "/{path:path}"),
        ("WS_PATH", "/ws?ticket=secret-value"),
        ("STARTUP_TIMEOUT", "nan"),
        ("SHUTDOWN_TIMEOUT", "inf"),
        ("SHUTDOWN_TIMEOUT", "0"),
        ("WS_MAX_QUEUE", "0"),
        ("WS_MAX_QUEUE", "65"),
        ("WS_MAX_SIZE", "131073"),
        ("WS_PING_INTERVAL", "0"),
        ("WS_PING_TIMEOUT", "-1"),
        ("LOG_LEVEL", "debug"),
        ("LOG_LEVEL", "trace"),
        ("ENABLE_SIGNALLING", "true"),
    ],
)
def test_invalid_config_fails_without_echoing_input(name, value):
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({"JRTC_SIGNALLING_" + name: value})
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": True},
        {"port": 8.5},
        {"startup_timeout": True},
        {"shutdown_timeout": float("nan")},
        {"host": None},
        {"websocket_path": None},
    ],
)
def test_programmatic_config_has_same_validation(kwargs):
    with pytest.raises(ConfigurationError):
        Settings(**kwargs)


def test_server_logs_cannot_leak_url_arguments_or_exception_input():
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "WebSocket %s denied",
        ("/ws?ticket=private-token",),
        (RuntimeError, RuntimeError("private-sdp"), None),
    )
    record.gateway_event = "private-event"
    rendered = SafeJSONFormatter().format(record)
    assert "private" not in rendered
    assert json.loads(rendered)["event"] == "server.log"
    record.gateway_event = "gateway.failed"
    assert json.loads(SafeJSONFormatter().format(record))["event"] == "gateway.failed"
