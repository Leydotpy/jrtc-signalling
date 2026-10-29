FROM ghcr.io/astral-sh/uv:0.13.4 AS uv
FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN groupadd --system gateway && useradd --system --gid gateway --no-create-home gateway
WORKDIR /app
COPY --from=builder --chown=gateway:gateway /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 JRTC_SIGNALLING_HOST=0.0.0.0
USER gateway
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('JRTC_SIGNALLING_PORT','8765')+'/health/live',timeout=2).close()"]
CMD ["jrtc-signalling"]
