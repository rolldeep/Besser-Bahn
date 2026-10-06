"""`bbt agent`: a Claude agent (Claude Agent SDK) over every ticket-tool
capability, traced to Langfuse.

    bbt agent "Cheapest Hildesheim → Berlin next week?"
    bbt agent "Weekend deals Hamburg ↔ Köln, next 3 weekends"
    bbt agent                                  # interactive chat
    bbt agent --notify "…"                     # unattended (cron): push the answer

Tools (in-process MCP server "bbt"): find_station, search_connections,
cheapest_fares, weekend_deals, booking_link, list_watches, add_watch,
remove_watch, check_watches, send_notification. Claude Code's built-in tools
(Bash, file edits, web …) are switched off: it can only use these.

Prices default to a BahnCard 25, 2nd class traveller.

Needs `pip install 'bbtickets[agent]'` (claude-agent-sdk, langfuse,
openinference-instrumentation-claude-agent-sdk) and ANTHROPIC_API_KEY.
Tracing turns on when LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY are set
(LANGFUSE_BASE_URL for self-hosted / EU / US cloud).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from datetime import datetime

from .agent_tools import TicketTools
from .vendo import BAHNCARDS, BERLIN, VendoError

DEFAULT_MODEL = "claude-opus-5-5"   # `bbt agent --model` / BBT_AGENT_MODEL
DEFAULT_EFFORT = "low"               # quick lookups; --effort / BBT_AGENT_EFFORT
SERVER = "bbt"

# -- tool schemas --------------------------------------------------------------

HHMM = {"type": "string", "pattern": r"^\d{2}:\d{2}$"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}
TRAVELLER = {
    "bahncard": {"type": "string", "enum": sorted(BAHNCARDS) + ["none"],
                 "description": "Default bc25 (BahnCard 25, 2nd class). Only "
                                "set when the user asks for something else."},
    "first_class": {"type": "boolean"},
    "adults": {"type": "integer", "minimum": 1, "maximum": 9},
    "max_transfers": {"type": "integer", "minimum": 0, "maximum": 5},
    "dticket": {"type": "boolean",
                "description": "Traveller holds a Deutschlandticket"},
}
ROUTE = {"origin": {"type": "string", "description": "Station name or HAFAS id"},
         "destination": {"type": "string"}}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


TOOLS = [
    ("find_station",
     "Look up DB stations by name. Only needed when a name is ambiguous or "
     "another tool reported no station found.",
     _schema({"query": {"type": "string"},
              "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
             ["query"])),
    ("search_connections",
     "Connections with live prices around one date/time (1 DB request). Use "
     "for a concrete day and time. Returns refs for booking_link.",
     _schema({**ROUTE, "date": DATE,
              "time": {**HHMM, "description": "HH:MM, default now / 06:00"},
              "arrive": {"type": "boolean",
                         "description": "time is the arrival time"},
              **TRAVELLER}, ["origin", "destination"])),
    ("cheapest_fares",
     "Cheapest fare per day over a date range plus the N cheapest trains "
     "across all days (1 DB request per day, Bestpreis calendar). Use for "
     "'cheapest ticket', 'when is it cheapest', flexible dates.",
     _schema({**ROUTE, "start_date": {**DATE, "description": "default today"},
              "days": {"type": "integer", "minimum": 1, "maximum": 31},
              "earliest": {**HHMM, "description": "departure from HH:MM"},
              "latest": {**HHMM, "description": "departure until HH:MM"},
              "top": {"type": "integer", "minimum": 0, "maximum": 20},
              **TRAVELLER}, ["origin", "destination"])),
    ("weekend_deals",
     "Quick weekend sale check: cheapest outbound (Fri/Sat) and return (Sun, "
     "optionally Mon) per upcoming weekend, with round-trip totals "
     "(~3 DB requests per weekend). Use for weekend trips / weekend deals.",
     _schema({**ROUTE,
              "weekends": {"type": "integer", "minimum": 1, "maximum": 6},
              "round_trip": {"type": "boolean", "description": "default true"},
              "include_friday": {"type": "boolean", "description": "default true"},
              "include_monday": {"type": "boolean"},
              "out_earliest": HHMM, "out_latest": HHMM,
              "back_earliest": HHMM, "back_latest": HHMM,
              "max_total": {"type": "number",
                            "description": "flag weekends at or under this total €"},
              **TRAVELLER}, ["origin", "destination"])),
    ("booking_link",
     "bahn.de booking link for one connection (by ref from a result). Opens "
     "that exact train; the user picks the fare and pays there. 1 DB request.",
     _schema({"ref": {"type": "string"}}, ["ref"])),
    ("list_watches",
     "Saved last-minute watches with their last check and cheapest price, "
     "and the configured notification channels.",
     _schema({}, [])),
    ("add_watch",
     "Save a watch for regular unattended checks: cron (`bbt check`) searches "
     "the route/day/window and pushes a notification when a fare appears or "
     "drops to max_price or below.",
     _schema({**ROUTE, "date": DATE, "earliest": HHMM, "latest": HHMM,
              "max_price": {"type": "number"},
              "train": {"type": "array", "items": {"type": "string"},
                        "description": "only these trains, e.g. ['ICE 597']"},
              "label": {"type": "string"}, **TRAVELLER},
             ["origin", "destination", "date"])),
    ("remove_watch", "Delete watches by id.",
     _schema({"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"])),
    ("check_watches",
     "Run the saved watches now (1–4 DB requests each). notify_matches=true "
     "also sends notifications and records them, like cron does.",
     _schema({"notify_matches": {"type": "boolean"},
              "id": {"type": "string", "description": "only this watch"}}, [])),
    ("send_notification",
     "Push a message to the user's configured channels (ntfy / Telegram / "
     "command). url becomes the tap action.",
     _schema({"title": {"type": "string"}, "message": {"type": "string"},
              "url": {"type": "string"}}, ["title", "message"])),
]

# How to use the tools well. Shared with `bbt mcp`, which sends it to MCP
# clients (LibreChat) as the server instructions.
GUIDE = """Traveller unless the user says otherwise: 1 adult, BahnCard 25, 2nd class. \
Prices from the tools already include the BahnCard 25 discount.

Every tool call costs DB requests (~2 s each; DB blocks bursts of ~10), so:
- Pass station names straight to the tools. Use find_station only when a name \
is ambiguous or a tool says no station was found.
- Flexible dates or "cheapest ticket" → cheapest_fares. One concrete day and \
time → search_connections. Weekend trips, weekend sale/deals → weekend_deals.
- Don't repeat a search whose results you already have. Get booking_link only \
for the one or two options you recommend.
- Watches (add_watch, list_watches, remove_watch, check_watches) are for \
regular checks that run from cron without you.
- If DB answers with a rate-limit or blocked error, say so; retry at most once.

Answer briefly: the cheapest option first (day, departure → arrival, train, \
changes, price in €) with its booking link, then up to four alternatives if \
they help. Say when a price is only known for a time slot."""

SYSTEM = """You are the Besser-Bahn ticket agent: you find the cheapest \
Deutsche Bahn tickets, fast.

Now: {now} (Europe/Berlin).
""" + GUIDE

UNATTENDED = """

This run is unattended (scheduled). Nobody can answer questions. When you \
have the answer, call send_notification once: title = route and best price, \
message = at most 5 short lines, url = the booking link of the best option."""


def system_prompt(notify: bool, now: datetime | None = None) -> str:
    now = now or datetime.now(BERLIN)
    return (SYSTEM.format(now=now.strftime("%A %Y-%m-%d %H:%M"))
            + (UNATTENDED if notify else ""))


# -- SDK glue ------------------------------------------------------------------

def _text(obj, is_error=False) -> dict:
    text = obj if isinstance(obj, str) else json.dumps(
        obj, ensure_ascii=False, default=str)
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def build_server(tools: TicketTools):
    """The in-process MCP server exposing every TicketTools method."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    def make(name, desc, schema):
        fn = getattr(tools, name)

        @tool(name, desc, schema)
        async def handler(args):
            try:
                # The DB client is blocking (and paced); keep the loop free.
                return _text(await asyncio.to_thread(fn, **(args or {})))
            except (VendoError, ValueError, TypeError) as e:
                return _text(f"error: {e}", is_error=True)
        return handler

    return create_sdk_mcp_server(
        name=SERVER, version="1.0.0",
        tools=[make(n, d, s) for n, d, s in TOOLS])


def build_options(tools: TicketTools, *, model: str, effort: str,
                  notify: bool, max_turns: int, max_budget_usd: float | None):
    from claude_agent_sdk import ClaudeAgentOptions
    return ClaudeAgentOptions(
        model=model,
        effort=effort,
        system_prompt=system_prompt(notify),
        mcp_servers={SERVER: build_server(tools)},
        tools=[],                       # no Bash/Read/Web… — only our tools
        allowed_tools=[f"mcp__{SERVER}__{n}" for n, _, _ in TOOLS],
        permission_mode="dontAsk",      # anything else is denied, never asked
        setting_sources=[],             # ignore ~/.claude + project settings
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
    )


# -- Langfuse ------------------------------------------------------------------

_langfuse = None


def langfuse_client():
    """Langfuse client with the Claude Agent SDK instrumented (OpenInference
    spans: one agent span per turn, one span per tool call, token usage and
    cost), or None when not configured / not installed."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse or None
    _langfuse = False
    if os.environ.get("BBT_AGENT_TRACE", "1") == "0" or not (
            os.environ.get("LANGFUSE_PUBLIC_KEY")
            and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse
        from langfuse.span_filter import is_default_export_span
        from openinference.instrumentation.claude_agent_sdk import \
            ClaudeAgentSDKInstrumentor
    except ImportError:
        print("note: Langfuse keys set but tracing deps missing — "
              "pip install 'bbtickets[agent]'", file=sys.stderr)
        return None

    def export(span) -> bool:
        # The MCP SDK opens its own root span per tools/call; the
        # instrumentor already records each call inside the agent's trace.
        scope = span.instrumentation_scope.name if span.instrumentation_scope else ""
        return scope != "mcp-python-sdk" and is_default_export_span(span)

    # Registers the global OTel tracer provider → Langfuse (keys/host from env).
    lf = Langfuse(should_export_span=export)
    ClaudeAgentSDKInstrumentor().instrument()
    _langfuse = lf
    return lf


@contextlib.contextmanager
def traced_turn(prompt: str, session_id: str, *, model: str, notify: bool):
    """One Langfuse trace per user turn, grouped by session. Yields a
    callback to record the final answer (or a no-op without Langfuse)."""
    lf = langfuse_client()
    if lf is None:
        yield lambda **_: None
        return
    from langfuse import propagate_attributes
    tags = ["bbt-agent", "bahncard25"] + (["unattended"] if notify else [])
    with propagate_attributes(session_id=session_id, tags=tags,
                              trace_name="bbt-agent",
                              metadata={"model": model}), \
            lf.start_as_current_observation(name="bbt-agent", as_type="agent",
                                            input=prompt) as span:
        yield lambda **kw: span.update(**kw)


# -- runner --------------------------------------------------------------------

async def _turn(client, prompt: str, session_id: str, *, model: str,
                notify: bool, verbose: bool, out=sys.stdout) -> bool:
    from claude_agent_sdk import (AssistantMessage, ResultMessage, TextBlock,
                                  ToolUseBlock)
    answer, ok = [], True
    with traced_turn(prompt, session_id, model=model, notify=notify) as record:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        answer.append(b.text)
                        print(b.text, file=out, flush=True)
                    elif isinstance(b, ToolUseBlock) and verbose:
                        name = b.name.removeprefix(f"mcp__{SERVER}__")
                        print(f"  → {name}({json.dumps(b.input, ensure_ascii=False)})",
                              file=sys.stderr, flush=True)
            elif isinstance(msg, ResultMessage):
                ok = not msg.is_error
                cost = (f", ${msg.total_cost_usd:.4f}"
                        if msg.total_cost_usd is not None else "")
                if verbose or not ok:
                    print(f"[{msg.subtype}: {msg.num_turns} turns, "
                          f"{msg.duration_ms / 1000:.1f}s{cost}]",
                          file=sys.stderr)
                record(output="\n".join(answer),
                       metadata={"cost_usd": msg.total_cost_usd,
                                 "num_turns": msg.num_turns,
                                 "subtype": msg.subtype},
                       **({} if ok else {"level": "ERROR",
                                          "status_message": msg.subtype}))
    return ok


async def run(prompt: str | None, *, model: str = DEFAULT_MODEL,
              effort: str = DEFAULT_EFFORT, notify: bool = False,
              bahncard: str | None = "bc25", max_turns: int = 20,
              max_budget_usd: float | None = None, verbose: bool = False,
              tools: TicketTools | None = None) -> int:
    from claude_agent_sdk import ClaudeSDKClient
    tools = tools or TicketTools(bahncard=bahncard)
    opts = build_options(tools, model=model, effort=effort, notify=notify,
                         max_turns=max_turns, max_budget_usd=max_budget_usd)
    session_id = f"bbt-{uuid.uuid4().hex[:12]}"
    ok = True
    try:
        async with ClaudeSDKClient(options=opts) as client:
            if prompt:
                ok = await _turn(client, prompt, session_id, model=model,
                                 notify=notify, verbose=verbose)
            else:
                print("Besser-Bahn ticket agent (BahnCard 25). Empty line or "
                      "Ctrl-D quits.", file=sys.stderr)
                while True:
                    try:
                        line = (await asyncio.to_thread(input, "\nyou> ")).strip()
                    except EOFError:
                        break
                    if not line:
                        break
                    ok = await _turn(client, line, session_id, model=model,
                                     notify=notify, verbose=verbose) and ok
    finally:
        lf = langfuse_client()
        if lf is not None:
            lf.flush()
    return 0 if ok else 1


def main(a, tools: TicketTools | None = None) -> int:
    """Entry point for `bbt agent` (argparse namespace from cli.py)."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        print("error: the agent needs extra packages — "
              "pip install 'bbtickets[agent]' (or `uv run --extra agent bbt agent …`)",
              file=sys.stderr)
        return 2
    prompt = " ".join(a.prompt).strip() or None
    if prompt is None and a.notify:
        print("error: --notify needs a prompt", file=sys.stderr)
        return 2
    bahncard = None if a.bahncard == "none" else a.bahncard
    return asyncio.run(run(prompt, model=a.model, effort=a.effort,
                           notify=a.notify, bahncard=bahncard,
                           max_turns=a.max_turns,
                           max_budget_usd=a.max_budget, verbose=a.verbose,
                           tools=tools))
