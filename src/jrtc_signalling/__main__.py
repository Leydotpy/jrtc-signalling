"""Supported entrypoint: python -m jrtc_signalling or jrtc-signalling."""

import argparse
import sys

import uvicorn

from .app import create_app
from .config import ConfigurationError, Settings
from .logging import log_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone JRTC signalling gateway")
    parser.add_argument("--check-config", action="store_true", help="validate environment and exit")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    if args.check_config:
        print("Configuration valid. Signalling remains disabled until integration is complete.")
        return
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        lifespan="on",
        ws="websockets",
        ws_max_size=settings.websocket_max_size,
        ws_max_queue=settings.websocket_max_queue,
        ws_ping_interval=settings.websocket_ping_interval,
        ws_ping_timeout=settings.websocket_ping_timeout,
        ws_per_message_deflate=False,
        timeout_graceful_shutdown=int(settings.shutdown_timeout) + 1,
        access_log=False,
        proxy_headers=False,
        ssl_certfile=settings.tls_cert_file or None,
        ssl_keyfile=settings.tls_key_file or None,
        log_config=log_config(settings.log_level),
    )


if __name__ == "__main__":
    main()
