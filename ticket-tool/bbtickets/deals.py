"""Fare finding on top of the Bestpreis calendar (`/angebote/tagesbestpreis`):
one request gives a whole day of prices, so a range of days or a few weekends
stays cheap on the /mob rate limit. Shared by `bbt hunt` and the agent."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .vendo import BERLIN, Connection, Trip, VendoClient, VendoError


class IntervalHit:
    """Bestpreis interval without connection detail — DB sometimes returns
    only the slot's price. Quacks enough like a Connection for a table row."""
    def __init__(self, iv):
        self.price, self.departure, self.arrival = iv["price"], iv["from"], iv["to"]
        self.trains, self.transfers = ["(time slot)"], 0


def _hhmm(dt: datetime | None) -> str:
    return dt.astimezone(BERLIN).strftime("%H:%M") if dt else ""


def _in_window(hhmm: str, earliest: str | None, latest: str | None) -> bool:
    return (not earliest or hhmm >= earliest) and (not latest or hhmm <= latest)


def day_best(client: VendoClient, trip: Trip, day: date,
             earliest: str | None = None, latest: str | None = None,
             pool: dict | None = None, not_before: datetime | None = None):
    """Cheapest bookable fare departing on `day` within [earliest, latest]
    (HH:MM, Berlin). Every priced connection seen goes into `pool` (by key).
    `not_before` drops departures already gone (for today). Returns a
    Connection, an IntervalHit, or None. Raises VendoError."""
    best = None
    for iv in client.best_prices(trip, day):
        for c in iv["connections"]:
            if not c.departure or c.price is None:
                continue
            if not_before and c.departure < not_before:
                continue
            if not _in_window(_hhmm(c.departure), earliest, latest):
                continue
            if pool is not None:
                pool.setdefault(c.key(), c)
            if best is None or c.price < best.price:
                best = c
        # Intervals may carry only the price, not priced connections.
        if not iv["connections"] and iv["price"] is not None and not iv["partial"]:
            if not_before and iv["to"] and iv["to"] < not_before:
                continue
            if _in_window(_hhmm(iv["from"]), earliest, latest):
                if best is None or iv["price"] < best.price:
                    best = IntervalHit(iv)
    return best


def cheapest_by_day(client: VendoClient, trip: Trip, start: date, days: int,
                    earliest: str | None = None, latest: str | None = None,
                    on_day=None, not_before: datetime | None = None):
    """The db-price-hunter idea with one request per day. Returns
    (rows, pool): rows = [(day, best | None, error | None)], pool = every
    priced connection seen, by key (for "N cheapest trains overall").
    `on_day(day, best, error)` is called as each day comes in."""
    rows, pool = [], {}
    for i in range(days):
        day = start + timedelta(days=i)
        try:
            best, err = day_best(client, trip, day, earliest, latest, pool,
                                 not_before), None
        except VendoError as e:
            best, err = None, str(e)
        rows.append((day, best, err))
        if on_day:
            on_day(day, best, err)
    return rows, pool


def cheapest_overall(pool: dict, n: int) -> list[Connection]:
    return sorted(pool.values(), key=lambda c: (c.price, c.departure))[:n]


def upcoming_weekends(today: date, count: int, include_friday: bool = True,
                      include_monday: bool = False) -> list[dict]:
    """The next `count` weekends as {"saturday", "out_days", "back_days"}.
    A weekend that has already started still counts (its past days are
    dropped), so on a Saturday "this weekend" is today + Sunday. On a
    Sunday there's no outbound day left, so it starts with next weekend."""
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    out = []
    for _ in range(count):
        outs = ([sat - timedelta(days=1)] if include_friday else []) + [sat]
        backs = [sat + timedelta(days=1)] + (
            [sat + timedelta(days=2)] if include_monday else [])
        out.append({"saturday": sat,
                    "out_days": [d for d in outs if d >= today],
                    "back_days": [d for d in backs if d >= today]})
        sat += timedelta(days=7)
    return out


def weekend_deals(client: VendoClient, trip: Trip, back_trip: Trip | None,
                  today: date, weekends: int = 2, include_friday: bool = True,
                  include_monday: bool = False,
                  out_earliest: str | None = None, out_latest: str | None = None,
                  back_earliest: str | None = None, back_latest: str | None = None,
                  now: datetime | None = None) -> list[dict]:
    """Cheapest outbound (Fri/Sat) and, with `back_trip`, cheapest return
    (Sun[/Mon]) per upcoming weekend. One Bestpreis request per day:
    a 2-weekend round trip is ~6 requests."""
    result = []
    for wk in upcoming_weekends(today, weekends, include_friday, include_monday):
        row = {"weekend_of": wk["saturday"], "out": None, "back": None,
               "errors": []}
        for d in wk["out_days"]:
            try:
                b = day_best(client, trip, d, out_earliest, out_latest,
                             not_before=now)
            except VendoError as e:
                row["errors"].append(f"{d}: {e}")
                continue
            if b and (row["out"] is None or b.price < row["out"].price):
                row["out"] = b
        if back_trip is not None:
            for d in wk["back_days"]:
                try:
                    b = day_best(client, back_trip, d, back_earliest,
                                 back_latest, not_before=now)
                except VendoError as e:
                    row["errors"].append(f"{d}: {e}")
                    continue
                if b and (row["back"] is None or b.price < row["back"].price):
                    row["back"] = b
        if row["out"] and (back_trip is None or row["back"]):
            row["total"] = round(row["out"].price
                                 + (row["back"].price if row["back"] else 0), 2)
        else:
            row["total"] = None
        result.append(row)
    return result
