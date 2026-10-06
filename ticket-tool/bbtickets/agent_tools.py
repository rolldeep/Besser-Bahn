"""The ticket tool's capabilities as plain functions for the agent
(`bbtickets.agent`). No SDK imports here, so it's testable offline: each
method takes JSON-ish arguments and returns a JSON-serialisable dict.

Every search defaults to a BahnCard 25 (2nd class) traveller, so prices are
what you'd actually pay with the card. Pass bahncard="none" for full fare.
"""
from __future__ import annotations

import threading
from datetime import date, datetime

from . import deals, notify, store, watcher
from .vendo import (BAHNCARDS, BERLIN, Connection, Station, Trip, VendoClient,
                    berlin)

DEFAULT_BAHNCARD = "bc25"
# Refs remembered for booking_link. A long-running `bbt mcp` server shares one
# TicketTools across all chats, so keep only the most recent ones.
MAX_REFS = 2000


def _local(dt: datetime | None) -> str | None:
    return dt.astimezone(BERLIN).strftime("%a %Y-%m-%d %H:%M") if dt else None


class TicketTools:
    def __init__(self, client: VendoClient | None = None, cfg: dict | None = None,
                 bahncard: str | None = DEFAULT_BAHNCARD, now=None):
        self.client = client or VendoClient()
        self.cfg = cfg if cfg is not None else store.load_config()
        self.bahncard = bahncard
        self._now = now or (lambda: datetime.now(BERLIN))
        # Connections handed to the model, by ref, so booking_link can mint
        # the exact-train link later without searching again.
        self._seen: dict[str, tuple[Connection, Trip]] = {}
        self._refs = 0
        self._lock = threading.Lock()  # the agent may run tools in parallel

    # -- helpers ---------------------------------------------------------------

    def _trip(self, origin: str, destination: str, bahncard: str | None = None,
              first_class: bool = False, adults: int = 1,
              max_transfers: int | None = None, dticket: bool = False) -> Trip:
        bc = self.bahncard if bahncard is None else bahncard
        if bc in ("", "none"):
            bc = None
        if bc is not None and bc not in BAHNCARDS:
            raise ValueError(f"unknown bahncard '{bc}' (use one of "
                             f"{', '.join(sorted(BAHNCARDS))} or 'none')")
        frm = self.client.resolve(origin)
        to = self.client.resolve(destination)
        return Trip(from_id=frm.location_id, to_id=to.location_id,
                    from_name=frm.name, to_name=to.name,
                    first_class=first_class, adults=max(1, int(adults or 1)),
                    bahncard=bc, dticket=dticket, max_transfers=max_transfers)

    @staticmethod
    def _flipped(t: Trip) -> Trip:
        return Trip(from_id=t.to_id, to_id=t.from_id, from_name=t.to_name,
                    to_name=t.from_name, first_class=t.first_class,
                    adults=t.adults, bahncard=t.bahncard, dticket=t.dticket,
                    max_transfers=t.max_transfers)

    def _conn(self, c, trip: Trip) -> dict | None:
        if c is None:
            return None
        if isinstance(c, deals.IntervalHit):
            return {"time_slot": f"{_local(c.departure)}–"
                                 f"{c.arrival.astimezone(BERLIN):%H:%M}"
                                 if c.arrival else _local(c.departure),
                    "price_eur": c.price,
                    "note": "DB only gave a price for this time slot; "
                            "use search_connections for that time to get trains"}
        with self._lock:
            self._refs += 1
            ref = f"c{self._refs}"
            self._seen[ref] = (c, trip)
            if len(self._seen) > MAX_REFS:
                del self._seen[next(iter(self._seen))]
        d = c.duration
        return {"ref": ref, "departure": _local(c.departure),
                "arrival": _local(c.arrival), "trains": c.trains,
                "transfers": c.transfers,
                "duration_min": int(d.total_seconds() // 60) if d else None,
                "price_eur": c.price, "delay_min": c.delay_min or None}

    @staticmethod
    def _traveller(t: Trip) -> str:
        bc = {"bc25": "BahnCard 25", "bc50": "BahnCard 50", "bc100": "BahnCard 100"}
        card = bc.get((t.bahncard or "").split("-")[0], "no BahnCard")
        cls = "1st" if t.first_class or (t.bahncard or "").endswith("-1") else "2nd"
        return f"{t.adults} adult(s), {card}, {cls} class"

    def _route(self, t: Trip) -> dict:
        return {"from": t.from_name, "to": t.to_name,
                "traveller": self._traveller(t)}

    # -- tools -----------------------------------------------------------------

    def find_station(self, query: str, limit: int = 5) -> dict:
        return {"stations": [s.to_dict() for s in
                             self.client.stations(query, limit=int(limit or 5))]}

    def search_connections(self, origin: str, destination: str,
                           date: str | None = None, time: str | None = None,
                           arrive: bool = False, **opts) -> dict:
        trip = self._trip(origin, destination, **opts)
        now = self._now()
        day = date or now.date().isoformat()
        hhmm = time or (now.strftime("%H:%M") if day == now.date().isoformat()
                        else "06:00")
        conns, _ = self.client.search(trip, berlin(day, hhmm), arrive=arrive)
        items = [self._conn(c, trip) for c in conns]
        priced = [i for i in items if i["price_eur"] is not None]
        cheapest = min(priced, key=lambda i: i["price_eur"]) if priced else None
        return {**self._route(trip), "searched": f"{day} {hhmm}"
                + (" (arrival)" if arrive else ""),
                "connections": items,
                "cheapest_ref": cheapest["ref"] if cheapest else None}

    def cheapest_fares(self, origin: str, destination: str,
                       start_date: str | None = None, days: int = 7,
                       earliest: str | None = None, latest: str | None = None,
                       top: int = 5, **opts) -> dict:
        days = max(1, min(int(days or 7), 31))
        trip = self._trip(origin, destination, **opts)
        now = self._now()
        start = date.fromisoformat(start_date) if start_date else now.date()
        rows, pool = deals.cheapest_by_day(self.client, trip, start, days,
                                           earliest, latest, not_before=now)
        per_day = [{"date": f"{d:%a %Y-%m-%d}",
                    "cheapest": self._conn(b, trip),
                    **({"error": e} if e else {})} for d, b, e in rows]
        return {**self._route(trip),
                "window": f"{earliest or '00:00'}–{latest or '23:59'}",
                "per_day": per_day,
                "cheapest_trains_overall": [
                    self._conn(c, trip)
                    for c in deals.cheapest_overall(pool, max(0, int(top or 0)))]}

    def weekend_deals(self, origin: str, destination: str, weekends: int = 2,
                      round_trip: bool = True, include_friday: bool = True,
                      include_monday: bool = False,
                      out_earliest: str | None = None,
                      out_latest: str | None = None,
                      back_earliest: str | None = None,
                      back_latest: str | None = None,
                      max_total: float | None = None, **opts) -> dict:
        weekends = max(1, min(int(weekends or 2), 6))
        trip = self._trip(origin, destination, **opts)
        back = self._flipped(trip) if round_trip else None
        now = self._now()
        rows = deals.weekend_deals(
            self.client, trip, back, now.date(), weekends, include_friday,
            include_monday, out_earliest, out_latest, back_earliest,
            back_latest, now=now)
        out = []
        for r in rows:
            item = {"weekend_of": f"Sat {r['weekend_of']:%Y-%m-%d}",
                    "outbound": self._conn(r["out"], trip),
                    "total_eur": r["total"]}
            if back is not None:
                item["return"] = self._conn(r["back"], back)
            if r["errors"]:
                item["errors"] = r["errors"]
            if max_total is not None and r["total"] is not None:
                item["within_budget"] = r["total"] <= float(max_total) + 1e-9
            out.append(item)
        totals = [i for i in out if i["total_eur"] is not None]
        best = min(totals, key=lambda i: i["total_eur"]) if totals else None
        return {**self._route(trip), "round_trip": round_trip,
                "weekends": out,
                "best_weekend": best["weekend_of"] if best else None,
                "best_total_eur": best["total_eur"] if best else None}

    def booking_link(self, ref: str) -> dict:
        if ref not in self._seen:
            raise ValueError(f"unknown or expired ref '{ref}' — use a ref "
                             "from a recent search result, or search again")
        c, trip = self._seen[ref]
        return {"ref": ref, "connection": watcher.fmt_conn(c),
                "url": self.client.booking_link(c, trip),
                "note": "Opens bahn.de for this train; pick the fare and pay "
                        "there. Check the BahnCard is set on the booking page."}

    def list_watches(self) -> dict:
        state = store.load_state()
        return {"watches": [
            {"id": w["id"], "label": w.get("label") or None,
             "description": watcher.describe(w),
             "bahncard": w.get("bahncard"),
             "last_check": state.get(w["id"], {}).get("last_check"),
             "cheapest_eur": state.get(w["id"], {}).get("cheapest"),
             "last_error": state.get(w["id"], {}).get("last_error")}
            for w in store.load_watches()],
            "notification_channels": notify.channels(self.cfg)}

    def add_watch(self, origin: str, destination: str, date: str,
                  earliest: str = "00:00", latest: str = "23:59",
                  max_price: float | None = None, train: list[str] | None = None,
                  label: str = "", **opts) -> dict:
        trip = self._trip(origin, destination, **opts)
        w = watcher.new_watch(
            frm=Station(trip.from_name, trip.from_id),
            to=Station(trip.to_name, trip.to_id), day=date, earliest=earliest or "00:00",
            latest=latest or "23:59", max_price=max_price,
            trains=train or [], max_transfers=trip.max_transfers,
            first_class=trip.first_class, adults=trip.adults,
            bahncard=trip.bahncard, dticket=trip.dticket, label=label or "")
        store.update_watches(lambda ws: ws + [w])
        return {"added": w["id"], "description": watcher.describe(w),
                "notification_channels": notify.channels(self.cfg),
                "note": "Checked by `bbt check` (cron: `bbt cron install`) or "
                        "`bbt serve --watch-every N`."}

    def remove_watch(self, ids: list[str]) -> dict:
        before = store.load_watches()
        after = store.update_watches(lambda ws: [w for w in ws if w["id"] not in ids])
        return {"removed": len(before) - len(after)}

    def check_watches(self, notify_matches: bool = False,
                      id: str | None = None) -> dict:
        results = watcher.run_checks(self.client, self.cfg,
                                     dry_run=not notify_matches, only=id,
                                     log=lambda *_: None)
        out = []
        for r in results:
            w = r["watch"]
            item = {"id": w["id"], "description": watcher.describe(w),
                    "status": r["status"]}
            if r.get("error"):
                item["error"] = r["error"]
            if r.get("matches"):
                item["matches"] = [self._conn(c, watcher.trip_of(w))
                                   for c in r["matches"][:5]]
            if r.get("fresh"):
                item["new_or_cheaper"] = len(r["fresh"])
            out.append(item)
        return {"results": out, "notified": notify_matches}

    def send_notification(self, title: str, message: str,
                          url: str | None = None) -> dict:
        ch = notify.channels(self.cfg)
        if not ch:
            raise ValueError("no notification channel configured — e.g. "
                             "`bbt config set notify.ntfy_topic <topic>`")
        errs = notify.send(self.cfg, title, message, url)
        return {"sent_via": ch, "errors": errs}
