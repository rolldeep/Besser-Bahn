"""Offline tests for deals.py, agent_tools.py and the agent's tool wiring.
The SDK-level test is skipped when claude-agent-sdk isn't installed."""
import asyncio
import inspect
import json
import pathlib
import sys
import unittest
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bbtickets import agent, deals, store  # noqa: E402
from bbtickets.agent_tools import TicketTools  # noqa: E402
from fakes import BERLIN_ID, MUENCHEN_ID, client, conn, day_at  # noqa: E402
from test_tickets import TempHome  # noqa: E402

# Wed 2030-05-08; that weekend is Fri 10. / Sat 11. / Sun 12.
NOW = day_at("2030-05-08", "09:00")


def bestpreis_transport(fake, prices):
    """tagesbestpreis answers from {(from_id_prefix, day): [prices]}; one
    connection per price, hourly from 08:00. Records requested days."""
    asked = []

    def transport(method, url, headers, data):
        if url.endswith("angebote/tagesbestpreis"):
            body = json.loads(data)
            w = body["reiseHin"]["wunsch"]
            day = w["zeitWunsch"]["reiseDatum"][:10]
            direction = "out" if w["abgangsLocationId"] == BERLIN_ID else "back"
            asked.append((direction, day,
                          body["reisendenProfil"]["reisende"][0]["ermaessigungen"][0]))
            ps = prices.get((direction, day), [])
            return 200, json.dumps({"tagesbestPreisIntervalle": [{
                "intervallAb": f"{day}T00:00:00+02:00",
                "intervallBis": f"{day}T23:59:00+02:00",
                "angebotsPreis": {"betrag": min(ps)} if ps else {},
                "verbindungen": [conn(day_at(day, f"{8 + i:02d}:00"), price=p)
                                 for i, p in enumerate(ps)]}]})
        return fake(method, url, headers, data)
    return transport, asked


class WeekendsTest(unittest.TestCase):
    def test_upcoming(self):
        wk = deals.upcoming_weekends(date(2030, 5, 8), 2)
        self.assertEqual(wk[0]["out_days"], [date(2030, 5, 10), date(2030, 5, 11)])
        self.assertEqual(wk[0]["back_days"], [date(2030, 5, 12)])
        self.assertEqual(wk[1]["saturday"], date(2030, 5, 18))

    def test_saturday_and_sunday(self):
        sat = deals.upcoming_weekends(date(2030, 5, 11), 1, include_monday=True)[0]
        self.assertEqual(sat["out_days"], [date(2030, 5, 11)])  # Fri is gone
        self.assertEqual(sat["back_days"], [date(2030, 5, 12), date(2030, 5, 13)])
        sun = deals.upcoming_weekends(date(2030, 5, 12), 1)[0]
        self.assertEqual(sun["saturday"], date(2030, 5, 18))    # next weekend


class ToolsTest(TempHome):
    def tools(self, prices=None):
        cl, fake = client()
        asked = None
        if prices is not None:
            cl._transport, asked = bestpreis_transport(fake, prices)
        return TicketTools(cl, store.load_config(), now=lambda: NOW), fake, asked

    def test_weekend_deals_round_trip_bc25(self):
        t, _, asked = self.tools({
            ("out", "2030-05-10"): [39.9, 24.9], ("out", "2030-05-11"): [19.9],
            ("back", "2030-05-12"): [29.9, 22.5],
            ("out", "2030-05-17"): [9.9], ("back", "2030-05-19"): [12.9],
        })
        r = t.weekend_deals(BERLIN_ID, MUENCHEN_ID, weekends=2, max_total=30)
        self.assertEqual(r["traveller"], "1 adult(s), BahnCard 25, 2nd class")
        w1, w2 = r["weekends"]
        self.assertEqual(w1["outbound"]["price_eur"], 19.9)
        self.assertEqual(w1["return"]["price_eur"], 22.5)
        self.assertEqual(w1["total_eur"], 42.4)
        self.assertFalse(w1["within_budget"])
        self.assertEqual(w2["total_eur"], 22.8)
        self.assertTrue(w2["within_budget"])
        self.assertEqual(r["best_weekend"], "Sat 2030-05-18")
        # 3 requests per weekend, all as BahnCard 25 travellers
        self.assertEqual(len(asked), 6)
        self.assertTrue(all(tok == "BAHNCARD25 KLASSE_2" for *_, tok in asked))

    def test_weekend_one_way_and_missing_return(self):
        t, _, asked = self.tools({("out", "2030-05-10"): [15.0]})
        r = t.weekend_deals(BERLIN_ID, MUENCHEN_ID, weekends=1, round_trip=False)
        self.assertEqual(r["weekends"][0]["total_eur"], 15.0)
        self.assertNotIn("return", r["weekends"][0])
        self.assertEqual({d for d, *_ in asked}, {"out"})
        r = t.weekend_deals(BERLIN_ID, MUENCHEN_ID, weekends=1)
        self.assertIsNone(r["weekends"][0]["total_eur"])  # no return fare

    def test_cheapest_fares_skips_departed_and_ranks(self):
        t, _, _ = self.tools({("out", "2030-05-08"): [5.0, 31.0, 30.0],
                              ("out", "2030-05-09"): [25.0]})
        r = t.cheapest_fares(BERLIN_ID, MUENCHEN_ID, days=2, top=2,
                             bahncard="none")
        self.assertEqual(r["traveller"], "1 adult(s), no BahnCard, 2nd class")
        # 08:00 €5 already left at NOW (09:00)
        self.assertEqual(r["per_day"][0]["cheapest"]["price_eur"], 30.0)
        self.assertEqual([c["price_eur"] for c in r["cheapest_trains_overall"]],
                         [25.0, 30.0])

    def test_search_then_booking_link(self):
        t, fake, _ = self.tools()
        fake.fahrplan_pages = [[conn(day_at("2030-05-10", "08:00"), price=40.0),
                                conn(day_at("2030-05-10", "09:00"), price=18.0)]]
        r = t.search_connections(BERLIN_ID, MUENCHEN_ID, date="2030-05-10",
                                 time="08:00")
        self.assertEqual(r["cheapest_ref"], r["connections"][1]["ref"])
        link = t.booking_link(r["cheapest_ref"])
        self.assertTrue(link["url"].endswith(fake.vbid))
        with self.assertRaises(ValueError):
            t.booking_link("c999")
        with self.assertRaises(ValueError):
            t.search_connections(BERLIN_ID, MUENCHEN_ID, bahncard="bc75")

    def test_watch_roundtrip(self):
        t, _, _ = self.tools()
        added = t.add_watch(BERLIN_ID, MUENCHEN_ID, date="2030-05-10",
                            earliest="07:00", latest="10:00", max_price=30)
        ws = t.list_watches()["watches"]
        self.assertEqual([w["id"] for w in ws], [added["added"]])
        self.assertEqual(ws[0]["bahncard"], "bc25")
        self.assertEqual(t.remove_watch([added["added"]])["removed"], 1)
        with self.assertRaises(ValueError):
            t.send_notification("x", "y")  # no channel configured


class WiringTest(unittest.TestCase):
    def test_every_tool_matches_a_method(self):
        for name, _, schema in agent.TOOLS:
            fn = getattr(TicketTools, name)
            params = inspect.signature(fn).parameters
            takes_opts = any(p.kind is p.VAR_KEYWORD for p in params.values())
            for prop in schema["properties"]:
                self.assertTrue(prop in params or takes_opts, f"{name}.{prop}")
            for req in schema["required"]:
                self.assertIn(req, params, f"{name} requires {req}")

    def test_system_prompt(self):
        p = agent.system_prompt(True, NOW)
        self.assertIn("Wednesday 2030-05-08 09:00", p)
        self.assertIn("BahnCard 25", p)
        self.assertIn("send_notification", p)
        self.assertNotIn("send_notification once", agent.system_prompt(False, NOW))


@unittest.skipUnless(__import__("importlib").util.find_spec("claude_agent_sdk"),
                     "claude-agent-sdk not installed")
class SdkTest(TempHome):
    def test_server_and_handler(self):
        cl, fake = client()
        t = TicketTools(cl, store.load_config(), now=lambda: NOW)
        opts = agent.build_options(t, model="m", effort="low", notify=False,
                                   max_turns=5, max_budget_usd=None)
        self.assertEqual(opts.tools, [])
        self.assertEqual(len(opts.allowed_tools), len(agent.TOOLS))
        server = opts.mcp_servers[agent.SERVER]
        self.assertEqual(server["type"], "sdk")
        # Call the handlers the way the SDK does.
        res = asyncio.run(_call(t, "find_station", {"query": "Berlin"}))
        self.assertFalse(res["is_error"])
        self.assertIn("Berlin Hbf", res["content"][0]["text"])
        res = asyncio.run(_call(t, "booking_link", {"ref": "nope"}))
        self.assertTrue(res["is_error"])


async def _call(tools, tool_name, args):
    """Build the server, capturing each tool's handler, then call one."""
    import claude_agent_sdk
    captured = {}
    orig = claude_agent_sdk.create_sdk_mcp_server

    def grab(name, version="1.0.0", tools=None):
        for tl in tools or []:
            captured[tl.name] = tl.handler
        return orig(name, version=version, tools=tools)
    claude_agent_sdk.create_sdk_mcp_server = grab
    try:
        agent.build_server(tools)
    finally:
        claude_agent_sdk.create_sdk_mcp_server = orig
    return await captured[tool_name](args)


if __name__ == "__main__":
    unittest.main()
