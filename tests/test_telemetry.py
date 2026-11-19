import asyncio
import json
import logging

import httpx2
import pytest
from media_helpers import Browser
from starlette.websockets import WebSocketDisconnect

from jrtc_signalling.app import create_app
from jrtc_signalling.config import ConfigurationError, Settings
from jrtc_signalling.logging import SafeJSONFormatter
from jrtc_signalling.telemetry import BOUNDS, Histogram, read_key, trace_ref


def test_histogram_has_constant_space_and_inclusive_cumulative_buckets():
    histogram = Histogram()
    for _ in range(2000):
        for value in (-1, float("nan"), float("inf"), 0.01, 61):
            histogram.observe(value)
    lines = histogram.render("delay").splitlines()
    assert len(lines) == len(BOUNDS) + 4
    assert 'delay_bucket{le="0.01"} 2000' in lines
    assert 'delay_bucket{le="+Inf"} 4000' in lines
    assert "delay_count 4000" in lines


async def test_scrape_handshake_resume_failures_and_private_trace(
    tmp_path, auth_config, authority, claims, sign, caplog
):
    token, secret = "t" * 32, "s" * 32
    for name, value in (("scrape", token), ("trace", secret)):
        (tmp_path / name).write_text(value)
    app = create_app(
        Settings(
            metrics_token_file=str(tmp_path / "scrape"), trace_key_file=str(tmp_path / "trace")
        ),
        auth_config=auth_config,
        authority_factory=lambda _: authority,
    )
    caplog.set_level(logging.INFO, logger="jrtc_signalling.session")
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://localhost"
        ) as client,
    ):
        header = {"Authorization": "Bearer " + token}
        assert (await client.get("/metrics")).status_code == 403
        assert (await client.get("/metrics?key=hidden", headers=header)).status_code == 403
        assert (await client.get("http://localhost/metrics", headers=header)).status_code == 403
        browser = Browser(app, claims, sign)
        assert (await browser.open("session.resume"))["ok"]
        await browser.close()
        bad = Browser(app, claims, lambda _: "not-a-ticket")
        with pytest.raises(WebSocketDisconnect):
            await bad.open("session.resume")
        await bad.close()
        await asyncio.sleep(0.3)
        response = await client.get("/metrics", headers=header)
        assert response.status_code == 200
        assert 'jrtc_handshakes_total{outcome="success"} 1' in response.text
        assert 'jrtc_resumes_total{outcome="failure"} 1' in response.text
        assert 'jrtc_authorization_errors_total{reason="unauthenticated"} 1' in response.text
        assert "jrtc_event_loop_lag_seconds_count 0\n" not in response.text
        [record] = [
            r for r in caplog.records if getattr(r, "gateway_event", "") == "gateway.resumed"
        ]
        rendered = SafeJSONFormatter().format(record)
        refs = json.loads(rendered)
        assert refs["meeting_ref"] == trace_ref(secret.encode(), "meeting", claims["meeting_id"])
        assert refs["session_ref"] != refs["meeting_ref"]
        for raw in (
            token,
            secret,
            claims["meeting_id"],
            claims["participant_session_id"],
            "not-a-ticket",
        ):
            assert raw not in rendered + response.text


async def test_metrics_default_off_and_invalid_key_fails_closed(tmp_path):
    app = create_app(Settings())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://localhost"
    ) as client:
        assert (await client.get("/metrics")).status_code == 404
    path = tmp_path / "key"
    for invalid in ("short", "x" * 257, "x" * 36 + "\nextra", "é" * 64):
        path.write_text(invalid)
        with pytest.raises(ConfigurationError):
            read_key(str(path))
