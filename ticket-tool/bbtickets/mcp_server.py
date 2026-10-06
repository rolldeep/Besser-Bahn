"""`bbt mcp`: every ticket-tool capability as an MCP server, so LibreChat (or
any other MCP client) can run the ticket agent with its own model, chat UI,
history and Langfuse tracing.

    bbt mcp                                         # stdio
    bbt mcp --http                                  # http://127.0.0.1:8738/mcp
    bbt mcp --http --host 0.0.0.0 --token <secret>  # e.g. in Docker next to LibreChat

Same tools, schemas and BahnCard 25 default as `bbt agent` (bbtickets.agent).
The tool guide goes out as the server instructions; LibreChat adds it to the
system prompt when the server has `serverInstructions: true`.

Needs `pip install 'bbtickets[mcp]'`. See librechat/ for the LibreChat setup.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import sys

from .agent import GUIDE, SERVER, TOOLS
from .agent_tools import TicketTools
from .vendo import VendoError

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _result(obj, is_error=False):
    import mcp.types as types
    text = obj if isinstance(obj, str) else json.dumps(
        obj, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                is_error=is_error)


def build_server(tools: TicketTools):
    """Low-level MCP server: TOOLS' JSON schemas as-is, each call run on the
    matching TicketTools method."""
    import mcp.types as types
    from mcp.server.lowlevel import Server

    listing = [types.Tool(name=n, description=d, input_schema=s)
               for n, d, s in TOOLS]
    names = {t.name for t in listing}

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=listing)

    async def call_tool(ctx, params):
        if params.name not in names:
            return _result(f"error: unknown tool '{params.name}'", is_error=True)
        fn = getattr(tools, params.name)
        try:
            # The DB client is blocking (and paced); keep the loop free.
            return _result(await asyncio.to_thread(fn, **(params.arguments or {})))
        except (VendoError, ValueError, TypeError) as e:
            return _result(f"error: {e}", is_error=True)

    return Server(SERVER, version="1.0.0", title="Besser-Bahn tickets",
                  instructions=GUIDE, on_list_tools=list_tools,
                  on_call_tool=call_tool)


def require_token(app, token: str):
    """ASGI wrapper: HTTP requests need `Authorization: Bearer <token>`."""
    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, expected):
                from starlette.responses import PlainTextResponse
                await PlainTextResponse("unauthorized", status_code=401)(
                    scope, receive, send)
                return
        await app(scope, receive, send)
    return guarded


def http_app(server, host: str, token: str | None):
    # Stateless: the tools keep no per-session state, and LibreChat simply
    # reconnects after a restart of this server.
    app = server.streamable_http_app(host=host, stateless_http=True)
    return require_token(app, token) if token else app


async def _stdio(server):
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(a, tools: TicketTools) -> int:
    """Entry point for `bbt mcp` (argparse namespace from cli.py)."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        print("error: the MCP server needs extra packages — "
              "pip install 'bbtickets[mcp]' (or `uv run --extra mcp bbt mcp …`)",
              file=sys.stderr)
        return 2
    server = build_server(tools)
    if not a.http:
        asyncio.run(_stdio(server))
        return 0
    if a.host not in LOOPBACK and not a.token:
        print("error: set --token (or BBT_MCP_TOKEN) before listening on "
              f"{a.host}: the tools can add watches and send notifications",
              file=sys.stderr)
        return 2
    import uvicorn
    print(f"bbt MCP server on http://{a.host}:{a.port}/mcp"
          + (" (bearer token required)" if a.token else ""), file=sys.stderr)
    uvicorn.run(http_app(server, a.host, a.token), host=a.host, port=a.port,
                log_level="warning")
    return 0
