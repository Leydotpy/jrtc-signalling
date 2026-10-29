"""Real loopback server/packaging smoke tests; no Janus or broker required."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect


def test_installed_entrypoint_rejects_invalid_config_safely():
    env = {**os.environ, "JRTC_SIGNALLING_PORT": "private-value"}
    result = subprocess.run(
        [sys.executable, "-m", "jrtc_signalling", "--check-config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "private-value" not in result.stderr


def test_package_runs_without_django():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util; assert importlib.util.find_spec('django') is None; from jrtc_signalling.app import create_app; create_app()",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_real_server_health_ws_denial_redaction_and_sigterm():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("JRTC_SIGNALLING_")
    }
    env.update(JRTC_SIGNALLING_PORT=str(port), JRTC_SIGNALLING_SHUTDOWN_TIMEOUT="2")
    process = subprocess.Popen(
        [sys.executable, "-m", "jrtc_signalling"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Gateway exited before becoming live")
            try:
                with urllib.request.urlopen(base + "/health/live", timeout=0.2) as response:
                    assert response.status == 200
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        else:
            pytest.fail("Gateway startup deadline exceeded")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/health/ready", timeout=2)
        assert error.value.code == 503
        assert json.loads(error.value.read())["signalling"] == "disabled"
        with (
            pytest.raises(InvalidStatus) as denied,
            connect(
                f"ws://127.0.0.1:{port}/ws?ticket=private-query-sentinel",
                origin="https://untrusted.test",
                open_timeout=2,
                proxy=None,
            ),
        ):
            pytest.fail("Unauthenticated WebSocket accepted")
        assert denied.value.response.status_code == 503
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail("Gateway did not stop within the shutdown budget")
    # Uvicorn re-raises the original signal after lifespan cleanup completes.
    assert process.returncode == -signal.SIGTERM
    assert "private-query-sentinel" not in stdout + stderr
    events = [json.loads(line)["event"] for line in stderr.splitlines() if line.startswith("{")]
    assert "gateway.started" in events
    assert events[-1] == "server.log" or events[-1] == "gateway.stopped"
    assert "gateway.stopped" in events
