# scotty-gateway image: Python gateway + Node (for npx-launched MCP servers)
# + uv/uvx (for Python MCP servers), so proxied servers stay config-only.
# Built and pushed by .github/workflows/scotty-gateway.yml (linux/amd64, linux/arm64).
FROM node:24.20.0-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM python:3.12.14-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npx --version
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Runs as the host user (compose `user:`); everything writable lives in /data:
# npm/uv caches under HOME, OAuth tokens, the audit log.
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/data/home \
    SCOTTY_GATEWAY_DATA=/data \
    SCOTTY_GATEWAY_CONFIG=/etc/scotty-gateway/gateway.json \
    PYTHONUNBUFFERED=1 \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off
EXPOSE 3050
ENTRYPOINT ["scotty-gateway"]
CMD ["serve"]
