# `bbt mcp` over HTTP, the tool server for LibreChat (see librechat/).
#   docker build -t bbt-mcp ticket-tool
#   docker run -e BBT_MCP_TOKEN=… -v ~/.config/besser-bahn-tickets:/data -p 127.0.0.1:8738:8738 bbt-mcp
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra mcp --no-install-project
COPY README.md ./
COPY bbtickets ./bbtickets
RUN uv sync --frozen --no-dev --extra mcp

# Any UID can run it (compose sets the host user); state lives in /data.
ENV PATH=/app/.venv/bin:$PATH HOME=/tmp BBT_HOME=/data
EXPOSE 8738
CMD ["bbt", "mcp", "--http", "--host", "0.0.0.0"]
