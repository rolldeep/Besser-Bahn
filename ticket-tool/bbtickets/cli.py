"""bbt — Besser-Bahn ticket tool.

  bbt search  Berlin München --date 2026-10-10 --time 08:00 [--book 2]
  bbt hunt    Berlin München --date 2026-10-10 --days 7
  bbt watch add Berlin München --date 2026-10-10 --earliest 07:00 --latest 11:00 --max-price 30
  bbt watch list | bbt watch rm ID
  bbt check                      # run all watches once (what cron calls)
  bbt cron install --every 10    # add the crontab line
  bbt serve                      # web UI on http://127.0.0.1:8737
  bbt config set notify.ntfy_topic my-secret-topic && bbt notify-test
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import webbrowser
from datetime import date, datetime, timedelta

from . import notify, store, watcher
from .vendo import BAHNCARDS, BERLIN, Trip, VendoClient, VendoError, berlin

CRON_MARK = "# besser-bahn-ticket-watch"
TOOL_DIR = pathlib.Path(__file__).resolve().parent.parent


def _trip(client: VendoClient, a, cfg) -> Trip:
    d = cfg["defaults"]
    frm, to = client.resolve(a.origin), client.resolve(a.destination)
    return Trip(from_id=frm.location_id, to_id=to.location_id,
                from_name=frm.name, to_name=to.name,
                first_class=(a.klasse == 1) if a.klasse else d["first_class"],
                adults=a.adults or d["adults"],
                bahncard=a.bahncard or d["bahncard"],
                dticket=a.dticket or d["dticket"],
                max_transfers=a.max_transfers)


def _when(a) -> datetime:
    day = a.date or date.today().isoformat()
    t = a.time or (datetime.now(BERLIN).strftime("%H:%M")
                   if day == date.today().isoformat() else "06:00")
    return berlin(day, t)


def _print_conns(conns, as_json=False):
    if as_json:
        print(json.dumps([c.to_dict() for c in conns], indent=2,
                         ensure_ascii=False))
        return
    if not conns:
        print("No connections found.")
        return
    for i, c in enumerate(conns, 1):
        delay = f" (+{c.delay_min})" if c.delay_min > 0 else ""
        print(f"{i:>2}. {watcher.fmt_conn(c)}{delay}")


def cmd_stations(a, client, cfg):
    for s in client.stations(a.query, limit=10):
        print(f"{s.name:<40} {s.type or '':<4} {s.eva or ''}")


def cmd_search(a, client, cfg):
    trip = _trip(client, a, cfg)
    print(f"{trip.from_name} → {trip.to_name}", file=sys.stderr)
    conns, _ = client.search(trip, _when(a), arrive=a.arrive)
    _print_conns(conns, a.json)
    pick = a.book
    if pick is None and conns and not a.json and sys.stdin.isatty():
        ans = input("\nBook which one? [number, Enter to skip] ").strip()
        pick = int(ans) if ans.isdigit() else None
    if pick:
        if not 1 <= pick <= len(conns):
            raise SystemExit(f"--book must be 1..{len(conns)}")
        _book(client, conns[pick - 1], trip, a.no_browser)


def _book(client, conn, trip, no_browser=False):
    link = client.booking_link(conn, trip)
    print(f"\n{watcher.fmt_conn(conn)}\nBook here (pick fare → pay): {link}")
    if not no_browser:
        webbrowser.open(link)


def cmd_hunt(a, client, cfg):
    """Cheapest price per day across a date range — the db-price-hunter idea,
    but one Bestpreis-calendar request per day instead of dozens of searches."""
    trip = _trip(client, a, cfg)
    start = date.fromisoformat(a.date) if a.date else date.today()
    rows = []
    print(f"{trip.from_name} → {trip.to_name}", file=sys.stderr)
    for i in range(a.days):
        day = start + timedelta(days=i)
        try:
            ivs = client.best_prices(trip, day)
        except VendoError as e:
            print(f"{day:%a %d.%m.}  error: {e}")
            continue
        best = None
        for iv in ivs:
            for c in iv["connections"]:
                dep = c.departure.astimezone(BERLIN) if c.departure else None
                if not dep or c.price is None:
                    continue
                if a.earliest and dep.strftime("%H:%M") < a.earliest:
                    continue
                if a.latest and dep.strftime("%H:%M") > a.latest:
                    continue
                if best is None or c.price < best.price:
                    best = c
            # Intervals may carry only the price, not priced connections.
            if not iv["connections"] and iv["price"] is not None and not iv["partial"]:
                frm = iv["from"].astimezone(BERLIN).strftime("%H:%M") if iv["from"] else ""
                if (not a.earliest or frm >= a.earliest) and (not a.latest or frm <= a.latest):
                    if best is None or iv["price"] < best.price:
                        best = _IntervalHit(iv)
        rows.append((day, best))
        print(f"{day:%a %d.%m.}  " + (watcher.fmt_conn(best) if best else "—"),
              flush=True)
    priced = [(d, b) for d, b in rows if b]
    if priced:
        d, b = min(priced, key=lambda r: r[1].price)
        print(f"\nCheapest: {d:%a %d.%m.} at €{b.price:.2f}")


class _IntervalHit:
    """Bestpreis interval without connection detail — enough for a table row."""
    def __init__(self, iv):
        self.price, self.departure, self.arrival = iv["price"], iv["from"], iv["to"]
        self.trains, self.transfers = ["(time slot)"], 0


def cmd_watch(a, client, cfg):
    if a.watch_cmd == "add":
        d = cfg["defaults"]
        frm, to = client.resolve(a.origin), client.resolve(a.destination)
        w = watcher.new_watch(
            frm=frm, to=to, day=a.date, earliest=a.earliest, latest=a.latest,
            max_price=a.max_price, trains=a.train or [],
            max_transfers=a.max_transfers,
            first_class=(a.klasse == 1) if a.klasse else d["first_class"],
            adults=a.adults or d["adults"], bahncard=a.bahncard or d["bahncard"],
            dticket=a.dticket or d["dticket"], label=a.label or "")
        store.update_watches(lambda ws: ws + [w])
        print(f"Watching [{w['id']}] {watcher.describe(w)}")
        if not notify.channels(cfg):
            print("Note: no notification channel configured — see "
                  "`bbt config set notify.ntfy_topic …`")
        if not _cron_line_installed():
            print("Note: cron not installed yet — run `bbt cron install`")
    elif a.watch_cmd == "rm":
        before = store.load_watches()
        after = store.update_watches(
            lambda ws: [w for w in ws if w["id"] not in a.ids])
        print(f"Removed {len(before) - len(after)} watch(es).")
    else:  # list
        state = store.load_state()
        ws = store.load_watches()
        if a.json:
            print(json.dumps([{**w, "state": state.get(w["id"], {})} for w in ws],
                             indent=2, ensure_ascii=False))
            return
        if not ws:
            print("No watches.")
        for w in ws:
            st = state.get(w["id"], {})
            extra = ""
            if st.get("last_check"):
                extra = f"  (checked {st['last_check'][11:16]}"
                if st.get("cheapest") is not None:
                    extra += f", cheapest €{st['cheapest']:.2f}"
                extra += ")"
            if st.get("last_error"):
                extra += f"  ⚠ {st['last_error']}"
            label = f"{w['label']}: " if w.get("label") else ""
            print(f"[{w['id']}] {label}{watcher.describe(w)}{extra}")


def cmd_check(a, client, cfg):
    log = (lambda *_: None) if a.quiet else print
    results = watcher.run_checks(client, cfg, dry_run=a.dry_run, only=a.id,
                                 log=log)
    if a.quiet:  # cron: only speak when something happened
        for r in results:
            if r.get("fresh") or r["status"] == "error":
                print(f"{datetime.now():%F %T} [{r['watch']['id']}] "
                      f"{r['status']} new={len(r.get('fresh', []))} "
                      f"{r.get('error', '')} {r.get('link') or ''}")
    if not results and not a.quiet:
        print("No watches. Add one with `bbt watch add …`.")


def _cron_command(every: int) -> str:
    env = f"BBT_HOME={shlex.quote(str(store.home()))} "
    cmd = (f"cd {shlex.quote(str(TOOL_DIR))} && {env}"
           f"{shlex.quote(sys.executable)} -m bbtickets check --quiet "
           f">> {shlex.quote(str(store.home() / 'check.log'))} 2>&1")
    sched = f"*/{every} * * * *" if every < 60 else f"0 */{every // 60} * * *"
    return f"{sched} {cmd} {CRON_MARK}"


def _crontab() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _cron_line_installed() -> bool:
    try:
        return CRON_MARK in _crontab()
    except FileNotFoundError:
        return False


def cmd_cron(a, client, cfg):
    line = _cron_command(a.every)
    if a.cron_cmd == "show":
        print(line)
        return
    try:
        current = [l for l in _crontab().splitlines() if CRON_MARK not in l]
    except FileNotFoundError:
        raise SystemExit("`crontab` not found. Add this line to your scheduler "
                         f"manually:\n{line}")
    if a.cron_cmd == "install":
        current.append(line)
    new = "\n".join(current).strip()
    subprocess.run(["crontab", "-"], input=(new + "\n") if new else "",
                   text=True, check=True)
    print(f"Installed: {line}" if a.cron_cmd == "install" else "Removed.")


def cmd_config(a, client, cfg):
    if a.config_cmd == "set":
        v = a.value
        v = {"true": True, "false": False, "null": None}.get(v.lower(), v)
        if isinstance(v, str) and v.isdigit():
            v = int(v)
        cfg = store.set_config(a.key, v)
    shown = json.loads(json.dumps(cfg))
    if shown["notify"].get("telegram_token"):
        shown["notify"]["telegram_token"] = "***"
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    print(f"\n(config file: {store.home() / 'config.json'})")


def cmd_notify_test(a, client, cfg):
    ch = notify.channels(cfg)
    if not ch:
        raise SystemExit("No channel configured. e.g. "
                         "`bbt config set notify.ntfy_topic <random-topic>`")
    errs = notify.send(cfg, "🚄 Besser-Bahn test",
                       "Notifications work. Tap to open bahn.de.",
                       "https://www.bahn.de")
    print("\n".join(errs) if errs else f"Sent via {', '.join(ch)}.")


def cmd_serve(a, client, cfg):
    from . import web
    web.serve(client, a.host, a.port, a.watch_every, a.token)


def _route_args(p, watch=False):
    p.add_argument("origin", help="from station (name or HAFAS id)")
    p.add_argument("destination", help="to station")
    p.add_argument("--date", required=watch, help="YYYY-MM-DD (default today)")
    p.add_argument("--class", dest="klasse", type=int, choices=[1, 2])
    p.add_argument("--bahncard", choices=sorted(BAHNCARDS))
    p.add_argument("--adults", type=int)
    p.add_argument("--dticket", action="store_true",
                   help="you hold a Deutschlandticket")
    p.add_argument("--max-transfers", type=int)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bbt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stations", help="look up station names")
    p.add_argument("query")
    p.set_defaults(fn=cmd_stations)

    p = sub.add_parser("search", help="search connections with prices")
    _route_args(p)
    p.add_argument("--time", help="HH:MM (default now / 06:00)")
    p.add_argument("--arrive", action="store_true", help="--time is arrival")
    p.add_argument("--book", type=int, metavar="N",
                   help="open the booking page for result N")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("hunt", help="cheapest price per day over a range")
    _route_args(p)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--earliest", help="HH:MM")
    p.add_argument("--latest", help="HH:MM")
    p.set_defaults(fn=cmd_hunt)

    p = sub.add_parser("watch", help="last-minute ticket watches")
    wsub = p.add_subparsers(dest="watch_cmd", required=True)
    pa = wsub.add_parser("add")
    _route_args(pa, watch=True)
    pa.add_argument("--earliest", default="00:00", help="HH:MM departure from")
    pa.add_argument("--latest", default="23:59", help="HH:MM departure until")
    pa.add_argument("--max-price", type=float, help="alert at or below (EUR)")
    pa.add_argument("--train", action="append",
                    help="only this train, e.g. 'ICE 597' (repeatable)")
    pa.add_argument("--label")
    pl = wsub.add_parser("list")
    pl.add_argument("--json", action="store_true")
    pr = wsub.add_parser("rm")
    pr.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("check", help="run all watches once (for cron)")
    p.add_argument("--quiet", action="store_true", help="log only events")
    p.add_argument("--dry-run", action="store_true",
                   help="don't notify or save state")
    p.add_argument("--id", help="only this watch")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("cron", help="install/remove the crontab entry")
    p.add_argument("cron_cmd", choices=["install", "remove", "show"])
    p.add_argument("--every", type=int, default=10,
                   help="minutes between checks (5..59, or a multiple of 60)")
    p.set_defaults(fn=cmd_cron)

    p = sub.add_parser("config", help="show/set config")
    csub = p.add_subparsers(dest="config_cmd")
    cs = csub.add_parser("set")
    cs.add_argument("key", help="e.g. notify.ntfy_topic, defaults.bahncard")
    cs.add_argument("value")
    csub.add_parser("show")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("notify-test", help="send a test notification")
    p.set_defaults(fn=cmd_notify_test)

    p = sub.add_parser("serve", help="lightweight web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--watch-every", type=int, default=0, metavar="MIN",
                   help="also run watch checks every MIN minutes (no cron needed)")
    p.add_argument("--token", default=os.environ.get("BBT_UI_TOKEN"),
                   help="require ?token=… (set this before using --host 0.0.0.0)")
    p.set_defaults(fn=cmd_serve)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "cron" and not (5 <= a.every < 60 or (a.every >= 60 and a.every % 60 == 0)):
        raise SystemExit("--every must be 5..59 or a multiple of 60 "
                         "(the DB backend rate-limits aggressive polling)")
    try:
        a.fn(a, VendoClient(), store.load_config())
    except (VendoError, ValueError) as e:  # ValueError: bad date/HH:MM
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
