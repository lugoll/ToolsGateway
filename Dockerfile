# tools-gateway image: Python only. uv is used to build the venv and is not in the final image.
# Built and pushed by .github/workflows/ci.yml (linux/amd64, linux/arm64).
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM python:3.12.14-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY gateway ./gateway
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.14-slim-bookworm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY catalog ./catalog
# Runs as the host user (compose `user:`); everything writable lives in /data
# (gateway.db: users, connections, encrypted secrets, audit log).
ENV PATH=/app/.venv/bin:$PATH \
    GATEWAY_DATA=/data \
    GATEWAY_CATALOG=/app/catalog \
    PYTHONUNBUFFERED=1 \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off
EXPOSE 3050
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3050/health/live', timeout=4)"
ENTRYPOINT ["gateway"]
CMD ["serve"]
