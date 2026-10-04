"""Lightweight web UI: one HTML page + a small JSON API, stdlib only.

Binds to 127.0.0.1 by default. To use it from your phone on the LAN, pass
--host 0.0.0.0 together with --token (or BBT_UI_TOKEN) and open
http://<pc>:8737/?token=<token>.
"""
from __future__ import annotations

import hmac
import json
import pathlib
import threading
import time
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import notify, store, watcher
from .vendo import (BAHNCARDS, BERLIN, Connection, Leg, Trip, VendoClient,
                    VendoError, berlin, parse_dt)

UI = pathlib.Path(__file__).with_name("ui.html")


def _trip(client: VendoClient, b: dict) -> tuple[Trip, dict, dict]:
    frm = client.resolve(str(b.get("from") or "").strip() or _bad("from missing"))
    to = client.resolve(str(b.get("to") or "").strip() or _bad("to missing"))
    bc = b.get("bahncard") or None
    if bc and bc not in BAHNCARDS:
        _bad(f"unknown bahncard {bc}")
    mt = b.get("max_transfers")
    trip = Trip(from_id=frm.location_id, to_id=to.location_id,
                from_name=frm.name, to_name=to.name,
                first_class=str(b.get("klasse")) == "1",
                adults=max(1, min(9, int(b.get("adults") or 1))),
                bahncard=bc, dticket=bool(b.get("dticket")),
                max_transfers=int(mt) if mt not in (None, "") else None)
    return trip, frm, to


class BadRequest(Exception):
    pass


def _bad(msg):
    raise BadRequest(msg)


def make_handler(client: VendoClient, token: str | None, ctx: dict):
    class H(BaseHTTPRequestHandler):
        server_version = "bbt"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -- plumbing ---------------------------------------------------------
        def _send(self, code: int, body, ctype="application/json"):
            data = (json.dumps(body, ensure_ascii=False, default=str).encode()
                    if ctype == "application/json" else body)
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, q) -> bool:
            if not token:
                # Tokenless = loopback only. Pin the Host header too, or a
                # DNS-rebinding page (evil.example → 127.0.0.1) would count as
                # same-origin and could drive the API.
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
                return host in ("127.0.0.1", "localhost", "[::1]")
            given = self.headers.get("X-BBT-Token") or (q.get("token") or [""])[0]
            return hmac.compare_digest(given, token)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                _bad("body too large")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                _bad("invalid JSON")

        def _dispatch(self, method):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                if not self._authed(q):
                    return self._send(401, b"Unauthorized - add ?token=...",
                                      "text/plain")
                return self._send(200, UI.read_bytes(), "text/html")
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authed(q):
                return self._send(401, {"error": "bad token"})
            # Same-origin only for writes: blocks other sites from driving the
            # API through your browser.
            origin = self.headers.get("Origin")
            if method != "GET" and origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._send(403, {"error": "cross-origin"})
            try:
                return self._send(200, self._route(method, u.path, q))
            except BadRequest as e:
                return self._send(400, {"error": str(e)})
            except (VendoError, ValueError) as e:
                return self._send(502 if isinstance(e, VendoError) else 400,
                                  {"error": str(e)})
            except Exception as e:  # pragma: no cover
                traceback.print_exc()
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # -- API --------------------------------------------------------------
        def _route(self, method, path, q):
            if method == "GET" and path == "/api/stations":
                term = (q.get("q") or [""])[0].strip()
                if len(term) < 2:
                    return []
                return [s.to_dict() for s in client.stations(term)]

            if method == "GET" and path == "/api/status":
                cfg = store.load_config()
                return {"channels": notify.channels(cfg),
                        "watch_every": ctx["watch_every"],
                        "last_auto_check": ctx.get("last_auto_check"),
                        "defaults": cfg["defaults"],
                        "bahncards": sorted(BAHNCARDS)}

            if method == "POST" and path == "/api/search":
                b = self._body()
                trip, frm, to = _trip(client, b)
                day = b.get("date") or date.today().isoformat()
                t = b.get("time") or datetime.now(BERLIN).strftime("%H:%M")
                conns, _ = client.search(trip, berlin(day, t),
                                         arrive=bool(b.get("arrive")))
                return {"from": frm.to_dict(), "to": to.to_dict(),
                        "connections": [c.to_dict() for c in conns]}

            if method == "POST" and path == "/api/book":
                b = self._body()
                trip, _, _ = _trip(client, b)
                dep = parse_dt(b.get("departure"))
                conn = Connection(
                    legs=[Leg(train="", origin=b.get("origin") or trip.from_name,
                              destination=b.get("destination") or trip.to_name,
                              departure=dep, arrival=None)],
                    price=None, kontext=b.get("kontext"))
                return {"url": client.booking_link(conn, trip)}

            if method == "GET" and path == "/api/watches":
                state = store.load_state()
                return [{**w, "state": state.get(w["id"], {})}
                        for w in store.load_watches()]

            if method == "POST" and path == "/api/watches":
                b = self._body()
                trip, frm, to = _trip(client, b)
                w = watcher.new_watch(
                    frm=frm, to=to, day=b.get("date") or _bad("date missing"),
                    earliest=b.get("earliest") or "00:00",
                    latest=b.get("latest") or "23:59",
                    max_price=b.get("max_price"),
                    trains=[t.strip() for t in str(b.get("train") or "").split(",")],
                    max_transfers=trip.max_transfers,
                    first_class=trip.first_class, adults=trip.adults,
                    bahncard=trip.bahncard, dticket=trip.dticket,
                    label=str(b.get("label") or "")[:60])
                store.update_watches(lambda ws: ws + [w])
                return w

            if method == "DELETE" and path.startswith("/api/watches/"):
                wid = path.rsplit("/", 1)[-1]
                store.update_watches(lambda ws: [w for w in ws if w["id"] != wid])
                return {"ok": True}

            if method == "POST" and path == "/api/check":
                b = self._body()
                res = watcher.run_checks(client, store.load_config(),
                                         only=b.get("id"), log=lambda *_: None)
                return [{"id": r["watch"]["id"], "status": r["status"],
                         "error": r.get("error"),
                         "matches": [c.to_dict() for c in r["matches"]],
                         "new": len(r.get("fresh", [])), "link": r.get("link")}
                        for r in res]

            if method == "POST" and path == "/api/notify-test":
                cfg = store.load_config()
                if not notify.channels(cfg):
                    _bad("no notification channel configured")
                errs = notify.send(cfg, "🚄 Besser-Bahn test",
                                   "Notifications work.", "https://www.bahn.de")
                return {"errors": errs}

            _bad(f"no route {method} {path}")

    return H


def _auto_checker(client: VendoClient, minutes: int, ctx: dict):
    while True:
        try:
            watcher.run_checks(client, store.load_config())
        except Exception:  # keep the loop alive
            traceback.print_exc()
        ctx["last_auto_check"] = datetime.now(BERLIN).isoformat(timespec="seconds")
        time.sleep(minutes * 60)


def serve(client: VendoClient, host="127.0.0.1", port=8737, watch_every=0,
          token: str | None = None):
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit("Refusing to listen on the network without --token "
                         "(anyone on the LAN could add watches / spam you).")
    ctx = {"watch_every": watch_every}
    if watch_every:
        if watch_every < 5:
            raise SystemExit("--watch-every must be ≥ 5 minutes (DB rate limit)")
        threading.Thread(target=_auto_checker, args=(client, watch_every, ctx),
                         daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(client, token, ctx))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    if token:
        url += f"?token={token}"
    print(f"Besser-Bahn tickets UI: {url}"
          + (f"  (auto-checking watches every {watch_every} min)" if watch_every else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
