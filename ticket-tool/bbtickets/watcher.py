"""Last-minute ticket watches.

A watch = route + day + departure window (+ optional price ceiling, train,
max transfers). Each `check` run (from cron) searches the window and notifies
when a bookable connection matches:

- a connection that wasn't matching before shows up (Sparpreis contingent
  released, a sold-out train bookable again, a new price under your ceiling);
- a connection you were already told about gets cheaper.

A connection that stops matching (sold out / price went up) is forgotten, so
it alerts again if it comes back. Watches delete themselves once the window
has passed.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from . import notify, store
from .vendo import BERLIN, Connection, Trip, VendoClient, VendoError, berlin

ERROR_ALERT_AFTER = 6  # consecutive failed runs before you get told


def new_watch(*, frm, to, day: str, earliest="00:00", latest="23:59",
              max_price=None, trains=None, max_transfers=None,
              first_class=False, adults=1, bahncard=None, dticket=False,
              label="") -> dict:
    berlin(day, earliest), berlin(day, latest)  # validate early
    return {
        "id": uuid.uuid4().hex[:6],
        "label": label,
        "from_id": frm.location_id, "from_name": frm.name,
        "to_id": to.location_id, "to_name": to.name,
        "date": day, "earliest": earliest, "latest": latest,
        "max_price": float(max_price) if max_price not in (None, "") else None,
        "trains": [t for t in (trains or []) if t.strip()],
        "max_transfers": max_transfers,
        "first_class": first_class, "adults": adults,
        "bahncard": bahncard, "dticket": dticket,
        "created": datetime.now(BERLIN).isoformat(timespec="seconds"),
    }


def trip_of(w: dict) -> Trip:
    return Trip(from_id=w["from_id"], to_id=w["to_id"],
                from_name=w.get("from_name", ""), to_name=w.get("to_name", ""),
                first_class=w.get("first_class", False),
                adults=w.get("adults", 1), bahncard=w.get("bahncard"),
                dticket=w.get("dticket", False),
                max_transfers=w.get("max_transfers"))


def window(w: dict) -> tuple[datetime, datetime]:
    start = berlin(w["date"], w.get("earliest") or "00:00")
    end = berlin(w["date"], w.get("latest") or "23:59")
    if end < start:  # e.g. 22:00–02:00 runs past midnight
        end += timedelta(days=1)
    return start, end


def describe(w: dict) -> str:
    s = f"{w['from_name']} → {w['to_name']}, {w['date']} {w['earliest']}–{w['latest']}"
    if w.get("max_price") is not None:
        s += f", ≤ €{w['max_price']:.2f}"
    if w.get("trains"):
        s += f", train {'/'.join(w['trains'])}"
    if w.get("max_transfers") is not None:
        s += f", ≤{w['max_transfers']} changes"
    return s


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def train_matches(wanted: list[str], trains: list[str]) -> bool:
    """'ICE 597', 'ice597' or just '597' match the leg 'ICE 597'."""
    if not wanted:
        return True
    have = {_norm(t) for t in trains}
    numbers = {m.group(0) for t in trains
               if (m := re.search(r"\d+$", t.strip()))}
    return any(_norm(x) in have or x.strip() in numbers for x in wanted)


def matches(w: dict, c: Connection) -> bool:
    if c.price is None or c.partial_price:
        return False  # nothing bookable to buy (sold out / not sold by DB)
    if w.get("max_price") is not None and c.price > w["max_price"] + 1e-9:
        return False
    if w.get("max_transfers") is not None and c.transfers > w["max_transfers"]:
        return False
    return train_matches(w.get("trains") or [], c.trains)


def fmt_conn(c: Connection) -> str:
    dep = c.departure.astimezone(BERLIN).strftime("%a %d.%m. %H:%M") if c.departure else "?"
    arr = c.arrival.astimezone(BERLIN).strftime("%H:%M") if c.arrival else "?"
    changes = "direct" if c.transfers == 0 else f"{c.transfers}× change"
    price = f"€{c.price:.2f}" if c.price is not None else "no price"
    return f"{dep} → {arr} · {', '.join(c.trains) or '?'} · {changes} · {price}"


def check_watch(client: VendoClient, w: dict, st: dict,
                now: datetime) -> dict:
    """Search one watch, update its slice of state `st` in place, return
    {status, matches, fresh}. `fresh` are the connections worth notifying."""
    start, end = window(w)
    if end <= now:
        return {"status": "expired", "matches": [], "fresh": []}
    conns = client.search_window(trip_of(w), max(start, now), end)
    hits = sorted((c for c in conns if matches(w, c)), key=lambda c: c.price)

    notified: dict = st.setdefault("notified", {})
    fresh = []
    for c in hits:
        before = notified.get(c.key())
        if before is None or c.price < before - 0.005:
            fresh.append(c)
    # Forget what no longer matches, so a comeback alerts again.
    st["notified"] = {c.key(): min(c.price, notified.get(c.key(), c.price))
                      for c in hits}
    for c in fresh:
        st["notified"][c.key()] = c.price
    st["last_check"] = now.isoformat(timespec="seconds")
    st["last_count"] = len(conns)
    st["cheapest"] = min((c.price for c in conns if c.price is not None),
                         default=None)
    st["errors"] = 0
    st.pop("last_error", None)
    return {"status": "ok", "matches": hits, "fresh": fresh}


def run_checks(client: VendoClient, cfg: dict, *, dry_run=False,
               only: str | None = None, now: datetime | None = None,
               log=print) -> list[dict]:
    """Check every watch once and notify. Called by cron (`bbt check`) and the
    web UI. Holds the store lock for the whole run."""
    now = now or datetime.now(BERLIN)
    results = []
    expired: set[str] = set()
    with store.lock("check"):
        watches = store.load_watches()
        state = store.load_state()
        for w in watches:
            if only and w["id"] != only:
                continue
            st = state.setdefault(w["id"], {})
            try:
                res = check_watch(client, w, st, now)
            except VendoError as e:
                st["errors"] = st.get("errors", 0) + 1
                st["last_error"] = str(e)
                log(f"[{w['id']}] error: {e}")
                if st["errors"] == ERROR_ALERT_AFTER and not dry_run:
                    notify.send(cfg, "⚠️ Ticket watch failing",
                                f"{describe(w)}\n{e}", priority=3)
                results.append({"watch": w, "status": "error", "error": str(e),
                                "matches": [], "fresh": []})
                continue

            if res["status"] == "expired":
                log(f"[{w['id']}] window passed — removing watch")
                expired.add(w["id"])
                results.append({"watch": w, **res})
                continue

            log(f"[{w['id']}] {describe(w)}: {len(res['matches'])} match(es), "
                f"{len(res['fresh'])} new"
                + (f", cheapest €{st['cheapest']:.2f}" if st.get("cheapest") else ""))
            link = None
            if res["fresh"]:
                best = res["fresh"][0]
                link = client.booking_link(best, trip_of(w))
                lines = [fmt_conn(c) for c in res["fresh"][:5]]
                if len(res["fresh"]) > 5:
                    lines.append(f"…and {len(res['fresh']) - 5} more")
                title = (f"🚄 €{best.price:.2f} {w['from_name']} → "
                         f"{w['to_name']}")
                if w.get("label"):
                    title = f"{w['label']}: {title}"
                for line in lines:
                    log(f"    {line}")
                log(f"    book: {link}")
                if not dry_run:
                    for err in notify.send(cfg, title, "\n".join(lines), link):
                        log(f"    notify failed — {err}")
            results.append({"watch": w, "link": link, **res})

        if not dry_run:
            # Re-read under the write lock: watches added/removed from the web
            # UI or CLI while this run was busy must survive.
            keep = store.update_watches(
                lambda current: [w for w in current if w["id"] not in expired])
            ids = {w["id"] for w in keep}
            store.save_state({k: v for k, v in state.items() if k in ids})
    return results
