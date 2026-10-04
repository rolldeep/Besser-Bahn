"""Offline tests: python3 -m unittest discover -s tests (from ticket-tool/)."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bbtickets import cli, store, watcher  # noqa: E402
from bbtickets.vendo import (Trip, VendoError, parse_connection,  # noqa: E402
                             search_link)
from fakes import BERLIN_ID, MUENCHEN_ID, client, conn, day_at, leg  # noqa: E402

DAY = "2030-05-10"


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("BBT_HOME")
        os.environ["BBT_HOME"] = self._tmp.name
        for k in store.ENV_OVERRIDES:
            os.environ.pop(k, None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BBT_HOME", None)
        else:
            os.environ["BBT_HOME"] = self._old
        self._tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_connection_fields(self):
        dep = day_at(DAY, "08:34")
        raw = conn(dep, trains=("ICE 597", "RE 3"), price=19.9)
        raw["verbindung"]["verbindungsAbschnitte"].insert(
            0, leg("", "Berlin Hbf", "Berlin Hbf (tief)", dep - timedelta(minutes=5),
                   dep, walk=True))
        c = parse_connection(raw)
        self.assertEqual(c.trains, ["ICE 597", "RE 3"])
        self.assertEqual(c.transfers, 1)
        self.assertEqual(c.price, 19.9)
        self.assertEqual(c.origin, "Berlin Hbf")
        self.assertEqual(c.destination, "München Hbf")
        self.assertIn("¶", c.kontext)
        self.assertTrue(c.key().endswith("ICE 597+RE 3"))

    def test_unpriced_and_delay(self):
        dep = day_at(DAY, "10:00")
        raw = conn(dep, price=None)
        raw["verbindung"]["verbindungsAbschnitte"][0] = leg(
            "ICE 1", "A", "B", dep, dep + timedelta(hours=1), delay=7)
        c = parse_connection(raw)
        self.assertIsNone(c.price)
        self.assertEqual(c.delay_min, 7)

    def test_train_name_fallback(self):
        dep = day_at(DAY, "10:00")
        a = leg("x", "A", "B", dep, dep + timedelta(hours=1))
        del a["mitteltext"]
        a["kurztext"], a["zugNummer"] = "IC", 2025
        c = parse_connection({"verbindung": {"verbindungsAbschnitte": [a]}})
        self.assertEqual(c.trains, ["IC 2025"])

    def test_search_link_fallback(self):
        c = parse_connection(conn(day_at(DAY, "08:34")))
        t = Trip(from_id=BERLIN_ID, to_id=MUENCHEN_ID, from_name="Berlin Hbf",
                 to_name="München Hbf", bahncard="bc50")
        url = search_link(c, t)
        self.assertTrue(url.startswith("https://www.bahn.de/buchung/fahrplan/suche#"))
        self.assertIn("hd=2030-05-10T08%3A34%3A00", url)
        self.assertIn("r=13%3A50%3AKLASSE_2%3A1", url)


class ClientTest(unittest.TestCase):
    def test_request_body_and_headers(self):
        cl, fake = client()
        seen = {}

        def transport(method, url, headers, data):
            seen.update(headers=headers, data=data)
            return fake(method, url, headers, data)
        cl._transport = transport
        t = Trip(from_id=BERLIN_ID, to_id=MUENCHEN_ID, adults=2, bahncard="bc25",
                 first_class=True, max_transfers=0)
        cl.search(t, day_at(DAY, "08:00"))
        body = fake.calls[0][2]
        self.assertEqual(body["klasse"], "KLASSE_1")
        self.assertEqual(len(body["reisendenProfil"]["reisende"]), 2)
        self.assertEqual(body["reisendenProfil"]["reisende"][0]["ermaessigungen"],
                         ["BAHNCARD25 KLASSE_2"])
        self.assertEqual(body["reiseHin"]["wunsch"]["maxUmstiege"], 0)
        self.assertEqual(body["reiseHin"]["wunsch"]["zeitWunsch"]["reiseDatum"],
                         "2030-05-10T08:00:00+02:00")
        self.assertIsInstance(seen["data"], bytes)  # charset suffix → 405
        self.assertEqual(seen["headers"]["Content-Type"],
                         "application/x.db.vendo.mob.verbindungssuche.v9+json")

    def test_retry_429_then_ok(self):
        cl, fake = client()
        fake.statuses = [429, 429]
        sleeps = []
        cl._sleep = sleeps.append
        cl.stations("Köln")
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(sleeps, [15, 30])

    def test_error_message_from_db(self):
        cl, fake = client()
        fake.statuses = [400]
        with self.assertRaisesRegex(VendoError, "kaputt"):
            cl.stations("Köln")

    def test_blocked(self):
        cl, fake = client()
        fake.statuses = [403]
        with self.assertRaisesRegex(VendoError, "curl_cffi"):
            cl.stations("Köln")

    def test_resolve_prefers_station_and_accepts_ids(self):
        cl, fake = client()
        self.assertEqual(cl.resolve("Köln").name, "Köln Hbf")
        s = cl.resolve(BERLIN_ID)
        self.assertEqual(s.name, "Berlin Hbf")
        self.assertEqual(len(fake.calls), 1)  # id needs no lookup

    def test_window_paginates_and_filters(self):
        cl, fake = client()
        fake.fahrplan_pages = [
            [conn(day_at(DAY, "07:50")), conn(day_at(DAY, "08:10"))],
            [conn(day_at(DAY, "09:00")), conn(day_at(DAY, "10:30"))],
            [conn(day_at(DAY, "12:00"))],
        ]
        t = Trip(from_id=BERLIN_ID, to_id=MUENCHEN_ID)
        res = cl.search_window(t, day_at(DAY, "08:00"), day_at(DAY, "10:00"))
        self.assertEqual([c.departure.strftime("%H:%M") for c in res],
                         ["08:10", "09:00"])
        self.assertEqual(len(fake.calls), 2)  # stopped once past the window

    def test_share_link(self):
        cl, fake = client()
        c = parse_connection(conn(day_at(DAY, "08:34")))
        self.assertEqual(cl.share_link(c),
                         f"https://www.bahn.de/buchung/start?vbid={fake.vbid}")
        self.assertEqual(fake.calls[0][2]["GH"], c.kontext)
        c.kontext = "43bc223b_3"  # checksum, not a recon ctx
        self.assertIsNone(cl.share_link(c))


class MatchTest(unittest.TestCase):
    def test_train_matches(self):
        self.assertTrue(watcher.train_matches([], ["ICE 597"]))
        self.assertTrue(watcher.train_matches(["ice597"], ["ICE 597"]))
        self.assertTrue(watcher.train_matches(["597"], ["ICE 597"]))
        self.assertFalse(watcher.train_matches(["97"], ["ICE 597"]))
        self.assertFalse(watcher.train_matches(["ICE 1597"], ["ICE 597"]))

    def test_matches(self):
        w = {"max_price": 30.0, "trains": [], "max_transfers": 0}
        self.assertTrue(watcher.matches(w, parse_connection(conn(day_at(DAY, "08:00"), price=30))))
        self.assertFalse(watcher.matches(w, parse_connection(conn(day_at(DAY, "08:00"), price=30.5))))
        self.assertFalse(watcher.matches(w, parse_connection(conn(day_at(DAY, "08:00"), price=None))))
        self.assertFalse(watcher.matches(
            w, parse_connection(conn(day_at(DAY, "08:00"), trains=("RE 1", "ICE 2"), price=5))))

    def test_window_over_midnight(self):
        s, e = watcher.window({"date": DAY, "earliest": "22:00", "latest": "01:30"})
        self.assertEqual((e - s), timedelta(hours=3, minutes=30))


class CheckTest(TempHome):
    def _watch(self, cl, **kw):
        w = watcher.new_watch(frm=cl.resolve(BERLIN_ID), to=cl.resolve(MUENCHEN_ID),
                              day=DAY, earliest="06:00", latest="12:00", **kw)
        store.update_watches(lambda ws: ws + [w])
        return w

    def _notify_to_file(self):
        out = pathlib.Path(self._tmp.name) / "notified.txt"
        store.set_config("notify.command",
                         f'printf "%s|%s\\n" "$BBT_TITLE" "$BBT_URL" >> "{out}"')
        return out

    def test_notify_once_then_on_drop_and_comeback(self):
        cl, fake = client()
        w = self._watch(cl, max_price=30)
        out = self._notify_to_file()
        now = day_at(DAY, "05:00")
        cfg = lambda: store.load_config()  # noqa: E731

        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=59.9)]]
        r = watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        self.assertEqual(r[0]["fresh"], [])
        self.assertFalse(out.exists())

        # Sparpreis contingent released → alert with the vbid booking link.
        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=27.9)]]
        watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        lines = out.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("€27.90", lines[0])
        self.assertIn("vbid=", lines[0])

        # Same price again → silent.
        watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        self.assertEqual(len(out.read_text().splitlines()), 1)

        # Cheaper → alert again.
        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=21.9)]]
        watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        self.assertEqual(len(out.read_text().splitlines()), 2)

        # Sold out, then back → alert again.
        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=None)]]
        watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=21.9)]]
        watcher.run_checks(cl, cfg(), now=now, log=lambda *_: None)
        self.assertEqual(len(out.read_text().splitlines()), 3)

        st = store.load_state()[w["id"]]
        self.assertEqual(st["cheapest"], 21.9)

    def test_dry_run_saves_nothing(self):
        cl, fake = client()
        self._watch(cl)
        out = self._notify_to_file()
        fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=10)]]
        watcher.run_checks(cl, store.load_config(), dry_run=True,
                           now=day_at(DAY, "05:00"), log=lambda *_: None)
        self.assertFalse(out.exists())
        self.assertEqual(store.load_state(), {})

    def test_expired_watch_removed_and_others_kept(self):
        cl, fake = client()
        self._watch(cl)
        watcher.run_checks(cl, store.load_config(), now=day_at(DAY, "13:00"),
                           log=lambda *_: None)
        self.assertEqual(store.load_watches(), [])
        self.assertEqual(fake.calls, [])  # no DB call for a dead window

    def test_errors_counted_and_alerted_once(self):
        cl, fake = client()
        w = self._watch(cl)
        out = self._notify_to_file()
        for _ in range(watcher.ERROR_ALERT_AFTER + 1):
            fake.statuses = [500]
            watcher.run_checks(cl, store.load_config(), now=day_at(DAY, "05:00"),
                               log=lambda *_: None)
        self.assertEqual(store.load_state()[w["id"]]["errors"],
                         watcher.ERROR_ALERT_AFTER + 1)
        self.assertEqual(len(out.read_text().splitlines()), 1)
        self.assertEqual(len(store.load_watches()), 1)  # kept

    def test_past_departures_ignored(self):
        cl, fake = client()
        self._watch(cl)
        fake.fahrplan_pages = [[conn(day_at(DAY, "07:00"), price=5),
                                conn(day_at(DAY, "09:00"), price=5)]]
        r = watcher.run_checks(cl, store.load_config(), dry_run=True,
                               now=day_at(DAY, "08:00"), log=lambda *_: None)
        self.assertEqual([c.departure.hour for c in r[0]["matches"]], [9])
        # The search itself starts at "now", not at the window start.
        self.assertEqual(fake.calls[0][2]["reiseHin"]["wunsch"]["zeitWunsch"]["reiseDatum"],
                         f"{DAY}T08:00:00+02:00")


class ConfigCronTest(TempHome):
    def test_config_env_and_set(self):
        store.set_config("notify.ntfy_topic", "abc")
        self.assertEqual(store.load_config()["notify"]["ntfy_topic"], "abc")
        os.environ["BBT_NTFY_TOPIC"] = "env"
        try:
            self.assertEqual(store.load_config()["notify"]["ntfy_topic"], "env")
        finally:
            del os.environ["BBT_NTFY_TOPIC"]
        with self.assertRaises(SystemExit):
            store.set_config("notify.nope", "x")

    def test_cron_line(self):
        line = cli._cron_command(10)
        self.assertTrue(line.startswith("*/10 * * * * cd "))
        self.assertIn("-m bbtickets check --quiet", line)
        self.assertIn(f"BBT_HOME={self._tmp.name}", line)
        self.assertTrue(line.endswith(cli.CRON_MARK))
        self.assertTrue(cli._cron_command(120).startswith("0 */2 * * *"))

    def test_cli_watch_add_list_rm(self):
        cl, fake = client()
        import io
        import contextlib
        from unittest import mock
        with mock.patch.object(cli, "VendoClient", lambda: cl), \
                mock.patch.object(cli, "_cron_line_installed", lambda: True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["watch", "add", "Berlin", "München", "--date", DAY,
                               "--max-price", "25", "--train", "ICE 597",
                               "--bahncard", "bc50"])
            self.assertEqual(rc, 0)
            ws = store.load_watches()
            self.assertEqual(ws[0]["from_name"], "Berlin Hbf")
            self.assertEqual(ws[0]["bahncard"], "bc50")
            self.assertEqual(ws[0]["trains"], ["ICE 597"])
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["watch", "rm", ws[0]["id"]])
            self.assertEqual(store.load_watches(), [])
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = cli.main(["watch", "add", "Berlin", "München", "--date", DAY,
                               "--earliest", "25:99"])
            self.assertEqual(rc, 2)
            self.assertIn("error:", err.getvalue())


class HuntTest(TempHome):
    def test_top_across_days(self):
        import contextlib
        import io
        from unittest import mock
        cl, fake = client()
        prices = {"2030-05-10": [49.9, 21.9], "2030-05-11": [17.9, 99.0]}

        def bestpreis(method, url, headers, data):
            if url.endswith("angebote/tagesbestpreis"):
                day = json.loads(data)["reiseHin"]["wunsch"]["zeitWunsch"]["reiseDatum"][:10]
                conns = [conn(day_at(day, f"{8 + i}:00"), price=p)
                         for i, p in enumerate(prices[day])]
                return 200, json.dumps({"tagesbestPreisIntervalle": [
                    {"intervallAb": f"{day}T00:00:00+02:00",
                     "intervallBis": f"{day}T23:59:00+02:00",
                     "angebotsPreis": {"betrag": min(prices[day])},
                     "verbindungen": conns}]})
            return fake(method, url, headers, data)
        cl._transport = bestpreis
        buf = io.StringIO()
        with mock.patch.object(cli, "VendoClient", lambda: cl), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["hunt", BERLIN_ID, MUENCHEN_ID, "--date", DAY,
                           "--days", "2", "--top", "3"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        top = out.split("cheapest trains across all days:")[1].strip().splitlines()
        self.assertEqual(len(top), 3)
        self.assertIn("€17.90", top[0])
        self.assertIn("€21.90", top[1])
        self.assertIn("€49.90", top[2])
        self.assertIn("Cheapest: Sat 11.05. at €17.90", out)


class WebTest(TempHome):
    def setUp(self):
        super().setUp()
        from http.server import ThreadingHTTPServer
        from bbtickets import web
        self.cl, self.fake = client()
        self.fake.fahrplan_pages = [[conn(day_at(DAY, "08:00"), price=19.9)]]
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            web.make_handler(self.cl, "s3cret", {"watch_every": 0}))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token="s3cret", headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        if token:
            h["X-BBT-Token"] = token
        r = urllib.request.Request(self.base + path, method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if path.startswith("/api") else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}") if path.startswith("/api") else None

    def test_page_needs_token(self):
        self.assertEqual(self.req("GET", "/", token=None)[0], 401)
        code, html = self.req("GET", "/?token=s3cret", token=None)
        self.assertEqual(code, 200)
        self.assertIn(b"Besser-Bahn Tickets", html)

    def test_search_book_watch_flow(self):
        code, r = self.req("POST", "/api/search",
                           {"from": "Berlin", "to": MUENCHEN_ID, "date": DAY,
                            "time": "07:00", "bahncard": "bc25"})
        self.assertEqual(code, 200, r)
        c = r["connections"][0]
        self.assertEqual(c["price"], 19.9)
        self.assertEqual(r["from"]["name"], "Berlin Hbf")

        code, b = self.req("POST", "/api/book",
                           {"from": r["from"]["id"], "to": r["to"]["id"],
                            "kontext": c["kontext"], "departure": c["departure"]})
        self.assertEqual(code, 200)
        self.assertIn("vbid=", b["url"])

        code, w = self.req("POST", "/api/watches",
                           {"from": r["from"]["id"], "to": r["to"]["id"],
                            "date": DAY, "earliest": "06:00", "latest": "09:00",
                            "max_price": "25", "train": "ICE 597, "})
        self.assertEqual(code, 200, w)
        self.assertEqual(w["trains"], ["ICE 597"])
        self.assertEqual(w["max_price"], 25.0)
        code, ws = self.req("GET", "/api/watches")
        self.assertEqual(len(ws), 1)
        code, _ = self.req("DELETE", f"/api/watches/{w['id']}")
        self.assertEqual(self.req("GET", "/api/watches")[1], [])

    def test_rejects_bad_token_cross_origin_and_bad_input(self):
        self.assertEqual(self.req("GET", "/api/watches", token="nope")[0], 401)
        code, _ = self.req("POST", "/api/watches", {"from": "x"},
                           headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, err = self.req("POST", "/api/search", {"from": "Berlin", "to": "Köln",
                                                     "bahncard": "bc99"})
        self.assertEqual(code, 400)
        self.assertIn("bahncard", err["error"])

    def test_tokenless_pins_loopback_host(self):
        from http.server import ThreadingHTTPServer
        from bbtickets import web
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(
            self.cl, None, {"watch_every": 0}))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/api/watches") as r:
                self.assertEqual(r.status, 200)
            rebind = urllib.request.Request(base + "/api/watches",
                                            headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(rebind)
            self.assertEqual(cm.exception.code, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_db_error_is_502(self):
        self.fake.statuses = [500]
        code, err = self.req("POST", "/api/search", {"from": BERLIN_ID, "to": MUENCHEN_ID})
        self.assertEqual(code, 502)
        self.assertIn("kaputt", err["error"])


if __name__ == "__main__":
    unittest.main()
