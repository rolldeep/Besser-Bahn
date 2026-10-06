"""Offline tests for `bbt mcp` (bbtickets/mcp_server.py) and the LibreChat
config in librechat/. Skipped when the `mcp` extra isn't installed."""
import asyncio
import importlib.util
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bbtickets import agent, agent_tools, store  # noqa: E402
from bbtickets.agent_tools import TicketTools  # noqa: E402
from fakes import client  # noqa: E402
from test_agent import NOW  # noqa: E402
from test_tickets import TempHome  # noqa: E402

HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_MCP, "mcp not installed (pip install 'bbtickets[mcp]')")
class McpServerTest(TempHome):
    def setUp(self):
        super().setUp()
        from bbtickets import mcp_server
        cl, _ = client()
        self.tools = TicketTools(cl, store.load_config(), now=lambda: NOW)
        self.server = mcp_server.build_server(self.tools)

    def session(self, fn):
        from mcp import Client

        async def go():
            async with Client(self.server) as c:
                return await fn(c)
        return asyncio.run(go())

    def test_lists_every_tool_with_its_schema(self):
        listed = self.session(lambda c: c.list_tools())
        got = {t.name: t.input_schema for t in listed.tools}
        self.assertEqual(got, {n: s for n, _, s in agent.TOOLS})

    def test_instructions_are_the_tool_guide(self):
        self.assertEqual(self.session(lambda c: asyncio.sleep(0, c.instructions)),
                         agent.GUIDE)

    def test_call_tool(self):
        res = self.session(lambda c: c.call_tool("find_station", {"query": "Berlin"}))
        self.assertFalse(res.is_error)
        self.assertIn("Berlin Hbf", json.loads(res.content[0].text)["stations"][0]["name"])

    def test_tool_error_is_reported_not_raised(self):
        res = self.session(lambda c: c.call_tool("booking_link", {"ref": "nope"}))
        self.assertTrue(res.is_error)
        self.assertIn("unknown or expired ref", res.content[0].text)

    def test_token_guard(self):
        from bbtickets import mcp_server

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        def status(headers):
            sent = []

            async def send(m):
                sent.append(m)

            async def receive():
                return {"type": "http.request", "body": b""}
            guarded = mcp_server.require_token(app, "s3cret")
            asyncio.run(guarded({"type": "http", "headers": headers}, receive, send))
            return sent[0]["status"]

        self.assertEqual(status([]), 401)
        self.assertEqual(status([(b"authorization", b"Bearer wrong")]), 401)
        self.assertEqual(status([(b"authorization", b"Bearer s3cret")]), 200)


class RefsTest(TempHome):
    def test_old_refs_expire(self):
        cl, _ = client()
        t = TicketTools(cl, store.load_config(), now=lambda: NOW)
        old = agent_tools.MAX_REFS
        agent_tools.MAX_REFS = 3
        try:
            c = SimpleNamespace(departure=NOW, arrival=NOW, trains=["ICE 597"],
                                transfers=0, duration=None, price=19.9,
                                delay_min=0)
            refs = [t._conn(c, None)["ref"] for _ in range(4)]
        finally:
            agent_tools.MAX_REFS = old
        self.assertLessEqual(len(t._seen), 3)
        self.assertNotIn(refs[0], t._seen)
        self.assertIn(refs[-1], t._seen)
        self.assertEqual(len(set(refs)), 4)        # refs are never reused


class LibreChatConfigTest(unittest.TestCase):
    """librechat/librechat.yaml must point at the server and tools we ship."""

    def setUp(self):
        self.yaml = (ROOT / "librechat" / "librechat.yaml").read_text()

    def test_mcp_server_and_spec(self):
        self.assertIn("  bbt:\n", self.yaml)
        self.assertIn("type: streamable-http", self.yaml)
        self.assertIn("url: http://bbt-mcp:8738/mcp", self.yaml)
        self.assertIn("'bbt-mcp:8738'", self.yaml)          # SSRF exemption
        self.assertIn('Bearer ${BBT_MCP_TOKEN}', self.yaml)
        self.assertIn('mcpServers: ["bbt"]', self.yaml)
        self.assertIn(f'model: "{agent.DEFAULT_MODEL}"', self.yaml)


if __name__ == "__main__":
    unittest.main()
