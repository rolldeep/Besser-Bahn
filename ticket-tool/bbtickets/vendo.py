"""Minimal client for the DB Vendo backend (`app.services-bahn.de/mob`).

The same API the DB Navigator app and the Besser-Bahn Flutter app use — see
flutter-app/lib/services/vendo_service.dart and api-tests/healthcheck.py for
the request/response shapes this mirrors. The bahn.de website API is
Akamai-blocked for non-browsers, so everything here goes through /mob.

Rate limit: /mob answers ~10 requests in ~6 s with a minutes-long 429 block,
so every call is paced (MIN_INTERVAL) and 429s back off geometrically.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

BASE = "https://app.services-bahn.de/mob"
JOURNEY_MEDIA = "application/x.db.vendo.mob.verbindungssuche.v9+json"
LOCATION_MEDIA = "application/x.db.vendo.mob.location.v3+json"
SHARE_MEDIA = "application/x.db.vendo.mob.verbindungteilen.v1+json"
APP_VERSION = "26.9.0"
USER_AGENT = f"DBNavigator/Android/{APP_VERSION}"
BERLIN = ZoneInfo("Europe/Berlin")

MIN_INTERVAL = 2.0
MAX_RETRIES = 3
TIMEOUT = 20

NO_DISCOUNT = "KEINE_ERMAESSIGUNG KLASSENLOS"
# CLI/UI shorthand → DB `ermaessigungen` token (flutter-app/lib/models/reisende.dart).
BAHNCARDS = {
    "bc25": "BAHNCARD25 KLASSE_2", "bc25-1": "BAHNCARD25 KLASSE_1",
    "bc50": "BAHNCARD50 KLASSE_2", "bc50-1": "BAHNCARD50 KLASSE_1",
    "bc100": "BAHNCARD100 KLASSE_2", "bc100-1": "BAHNCARD100 KLASSE_1",
}

# curl_cffi (real Chrome TLS) gets through Akamai where plain `requests` is
# fingerprinted and answered with OPS_BLOCKED — same reasoning as healthcheck.py.
try:
    from curl_cffi import requests as _cc
except Exception:  # pragma: no cover - optional dependency
    _cc = None
import requests


class VendoError(Exception):
    pass


@dataclass
class Station:
    name: str
    location_id: str
    eva: str | None = None
    type: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "id": self.location_id, "eva": self.eva,
                "type": self.type}


@dataclass
class Trip:
    """What to search: route + who travels. Serialisable into a watch."""
    from_id: str
    to_id: str
    from_name: str = ""
    to_name: str = ""
    first_class: bool = False
    adults: int = 1
    bahncard: str | None = None   # key of BAHNCARDS, e.g. "bc25"
    dticket: bool = False
    max_transfers: int | None = None

    def reisende(self) -> list[dict]:
        token = BAHNCARDS.get(self.bahncard or "", NO_DISCOUNT)
        return [{"ermaessigungen": [token], "reisendenTyp": "ERWACHSENER"}
                for _ in range(max(1, self.adults))]

    def wunsch(self, when: datetime, arrive: bool = False,
               context: str | None = None) -> dict:
        w = {
            "abgangsLocationId": self.from_id,
            "alternativeHalteBerechnung": True,
            "verkehrsmittel": ["ALL"],
            "zeitWunsch": {"reiseDatum": iso(when),
                           "zeitPunktArt": "ANKUNFT" if arrive else "ABFAHRT"},
            "zielLocationId": self.to_id,
        }
        if self.max_transfers is not None:
            w["maxUmstiege"] = self.max_transfers
        if context:
            w["context"] = context
        return w

    def body(self, when: datetime, arrive: bool = False,
             context: str | None = None) -> dict:
        return {
            "autonomeReservierung": False,
            "einstiegsTypList": ["STANDARD"],
            "fahrverguenstigungen": {
                "deutschlandTicketVorhanden": self.dticket,
                "nurDeutschlandTicketVerbindungen": False,
            },
            "klasse": "KLASSE_1" if self.first_class else "KLASSE_2",
            "reiseHin": {"wunsch": self.wunsch(when, arrive, context)},
            "reisendenProfil": {"reisende": self.reisende()},
            "reservierungsKontingenteVorhanden": False,
        }


@dataclass
class Leg:
    train: str           # "ICE 597", "RE 7" … ("" for walks)
    origin: str
    destination: str
    departure: datetime | None
    arrival: datetime | None
    rt_departure: datetime | None = None
    rt_arrival: datetime | None = None
    walk: bool = False


@dataclass
class Connection:
    legs: list[Leg]
    price: float | None
    currency: str = "EUR"
    kontext: str | None = None
    partial_price: bool = False
    raw: dict = field(default=None, repr=False)

    @property
    def rides(self) -> list[Leg]:
        return [l for l in self.legs if not l.walk]

    @property
    def departure(self) -> datetime | None:
        return self.legs[0].departure if self.legs else None

    @property
    def arrival(self) -> datetime | None:
        return self.legs[-1].arrival if self.legs else None

    @property
    def origin(self) -> str:
        return self.legs[0].origin if self.legs else ""

    @property
    def destination(self) -> str:
        return self.legs[-1].destination if self.legs else ""

    @property
    def trains(self) -> list[str]:
        return [l.train for l in self.rides if l.train]

    @property
    def transfers(self) -> int:
        return max(0, len(self.rides) - 1)

    @property
    def duration(self) -> timedelta | None:
        if self.departure and self.arrival:
            return self.arrival - self.departure
        return None

    @property
    def delay_min(self) -> int:
        """Realtime departure delay of the first ride, in minutes."""
        r = self.rides[0] if self.rides else None
        if r and r.departure and r.rt_departure:
            return int((r.rt_departure - r.departure).total_seconds() // 60)
        return 0

    def key(self) -> str:
        """Stable identity across polls: planned departure + trains."""
        dep = self.departure.isoformat() if self.departure else "?"
        return f"{dep}|{'+'.join(self.trains)}"

    def to_dict(self) -> dict:
        d = self.duration
        return {
            "key": self.key(),
            "departure": self.departure.isoformat() if self.departure else None,
            "arrival": self.arrival.isoformat() if self.arrival else None,
            "origin": self.origin, "destination": self.destination,
            "trains": self.trains, "transfers": self.transfers,
            "duration_min": int(d.total_seconds() // 60) if d else None,
            "delay_min": self.delay_min,
            "price": self.price, "currency": self.currency,
            "partial_price": self.partial_price,
            "kontext": self.kontext,
        }


# -- helpers -----------------------------------------------------------------

def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    return dt.replace(microsecond=0).isoformat()


def parse_dt(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=BERLIN)


def berlin(day: date | str, hhmm: str = "00:00") -> datetime:
    if isinstance(day, str):
        day = date.fromisoformat(day)
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=BERLIN)


def parse_leg(a: dict) -> Leg:
    walk = a.get("typ") == "FUSSWEG"
    train = ""
    if not walk:
        train = (a.get("mitteltext") or "").strip()
        if not train:
            num = a.get("zugNummer") or a.get("verkehrsmittelNummer") or ""
            train = f"{a.get('kurztext') or ''} {num}".strip()
    dep, arr = parse_dt(a.get("abgangsDatum")), parse_dt(a.get("ankunftsDatum"))
    return Leg(
        train=train,
        origin=(a.get("abgangsOrt") or {}).get("name", ""),
        destination=(a.get("ankunftsOrt") or {}).get("name", ""),
        departure=dep, arrival=arr,
        rt_departure=parse_dt(a.get("ezAbgangsDatum")) or dep,
        rt_arrival=parse_dt(a.get("ezAnkunftsDatum")) or arr,
        walk=walk,
    )


def parse_connection(c: dict) -> Connection:
    """One element of `verbindungen` (fahrplan / tagesbestpreis / recon)."""
    vb = c.get("verbindung") or c
    legs = [parse_leg(a) for a in vb.get("verbindungsAbschnitte") or []
            if isinstance(a, dict)]
    ab = (((c.get("angebote") or {}).get("preise") or {})
          .get("gesamt") or {}).get("ab") or {}
    price = ab.get("betrag")
    return Connection(
        legs=legs,
        price=float(price) if isinstance(price, (int, float)) else None,
        currency=ab.get("waehrung") or "EUR",
        kontext=vb.get("kontext"),
        raw=c,
    )


def search_link(conn: Connection, trip: Trip) -> str:
    """Fallback bahn.de deep link: a pre-filled search at this departure
    (same format as the repo's main.py). Used when /teilen can't mint a vbid."""
    hd = conn.departure.strftime("%Y-%m-%dT%H:%M:%S") if conn.departure else ""
    params = {
        "sts": "true", "so": trip.from_name or conn.origin,
        "zo": trip.to_name or conn.destination,
        "soid": trip.from_id, "zoid": trip.to_id, "hd": hd,
        "dltv": str(trip.dticket).lower(),
    }
    r = {"bc25": "13:25:KLASSE_2:1", "bc25-1": "13:25:KLASSE_1:1",
         "bc50": "13:50:KLASSE_2:1", "bc50-1": "13:50:KLASSE_1:1"}.get(
             trip.bahncard or "")
    if r:
        params["r"] = r
    frag = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"https://www.bahn.de/buchung/fahrplan/suche#{frag}"


# -- client ------------------------------------------------------------------

class VendoClient:
    def __init__(self, min_interval: float = MIN_INTERVAL, sleep=time.sleep,
                 transport=None):
        self.min_interval = min_interval
        self._sleep = sleep
        self._transport = transport or _default_transport
        self._lock = threading.Lock()   # the web UI calls from many threads
        self._last = 0.0
        self._station_cache: dict[str, list[Station]] = {}

    def _headers(self, media: str) -> dict:
        return {
            "Accept": media, "Content-Type": media, "Accept-Language": "de",
            "User-Agent": USER_AGENT, "X-App-Version": APP_VERSION,
            "X-Correlation-ID": f"{uuid.uuid4()}_{uuid.uuid4()}",
        }

    def call(self, method: str, path: str, media: str, body=None):
        url = f"{BASE}/{path}"
        # Bytes, not str: the DB edge exact-matches the media type and rejects
        # a `; charset=utf-8` suffix with 405 (see vendo_service.dart).
        data = json.dumps(body).encode() if body is not None else None
        with self._lock:
            for attempt in range(MAX_RETRIES + 1):
                gap = time.monotonic() - self._last
                if gap < self.min_interval:
                    self._sleep(self.min_interval - gap)
                self._last = time.monotonic()
                status, text = self._transport(method, url, self._headers(media),
                                               data)
                if status != 429 or attempt == MAX_RETRIES:
                    break
                # Retry-After understates a sustained block; back off harder.
                self._sleep(min(15 * (attempt + 1), 60))
        if status == 429:
            raise VendoError("DB rate limit (429) — try again in a few minutes")
        if status in (403, 452) or "OPS_BLOCKED" in text[:500]:
            raise VendoError(
                f"DB edge blocked the request ({status}). Install curl_cffi "
                "(pip install curl_cffi) or run from a residential connection.")
        if not 200 <= status < 300:
            raise VendoError(_db_message(text) or
                             f"HTTP {status} from /{path}: {text[:200]}")
        try:
            return json.loads(text) if text else {}
        except ValueError as e:
            raise VendoError(f"non-JSON answer from /{path}") from e

    # -- endpoints -------------------------------------------------------------

    def stations(self, query: str, limit: int = 8) -> list[Station]:
        q = query.strip()
        if q in self._station_cache:
            return self._station_cache[q][:limit]
        data = self.call("POST", "location/search", LOCATION_MEDIA,
                         {"locationTypes": ["ALL"], "searchTerm": q})
        out = [Station(name=o.get("name", ""), location_id=o["locationId"],
                       eva=o.get("evaNr"), type=o.get("locationType"))
               for o in (data if isinstance(data, list) else [])
               if isinstance(o, dict) and o.get("locationId")]
        # Stops first: an address with the same name is rarely what you mean.
        out.sort(key=lambda s: 0 if s.type == "ST" or s.eva else 1)
        self._station_cache[q] = out
        return out[:limit]

    def resolve(self, query: str) -> Station:
        """A station name or a full HAFAS location id → Station."""
        if "@L=" in query or query.startswith("A="):
            name = query.split("@O=", 1)[-1].split("@", 1)[0]
            return Station(name=name, location_id=query)
        hits = self.stations(query, limit=1)
        if not hits:
            raise VendoError(f"no station found for '{query}'")
        return hits[0]

    def search(self, trip: Trip, when: datetime, arrive: bool = False,
               context: str | None = None) -> tuple[list[Connection], str | None]:
        data = self.call("POST", "angebote/fahrplan", JOURNEY_MEDIA,
                         trip.body(when, arrive, context))
        conns = [parse_connection(c) for c in data.get("verbindungen") or []
                 if isinstance(c, dict)]
        return [c for c in conns if c.legs], data.get("spaeterContext")

    def search_window(self, trip: Trip, start: datetime, end: datetime,
                      max_pages: int = 4) -> list[Connection]:
        """All connections departing in [start, end], paging with
        spaeterContext. Capped — each page is one rate-limited request."""
        seen: dict[str, Connection] = {}
        context = None
        for _ in range(max_pages):
            conns, context = self.search(trip, start, context=context)
            for c in conns:
                if c.departure and start <= c.departure <= end:
                    seen.setdefault(c.key(), c)
            last = max((c.departure for c in conns if c.departure), default=None)
            if not context or not conns or (last and last >= end):
                break
        return sorted(seen.values(), key=lambda c: c.departure)

    def best_prices(self, trip: Trip, day: date) -> list[dict]:
        """Bestpreis calendar for one day (one request): per time interval the
        cheapest price plus its connections."""
        data = self.call("POST", "angebote/tagesbestpreis", JOURNEY_MEDIA,
                         trip.body(berlin(day)))
        out = []
        for iv in data.get("tagesbestPreisIntervalle") or []:
            p = (iv.get("angebotsPreis") or {}).get("betrag")
            out.append({
                "from": parse_dt(iv.get("intervallAb")),
                "to": parse_dt(iv.get("intervallBis")),
                "price": float(p) if isinstance(p, (int, float)) else None,
                "best": bool(iv.get("istBestpreis")),
                "partial": bool(iv.get("istTeilpreis")),
                "connections": [parse_connection(c)
                                for c in iv.get("verbindungen") or []],
            })
        return out

    def share_link(self, conn: Connection) -> str | None:
        """bahn.de link to the EXACT connection (same as DB Navigator's
        "Reise teilen") — opens booking for this train, ready to pay."""
        if not conn.kontext or "¶" not in conn.kontext:
            return None
        body = {"GH": conn.kontext,
                "HD": iso(conn.departure) if conn.departure else None,
                "SO": conn.origin, "ZO": conn.destination}
        try:
            data = self.call("POST", "angebote/verbindung/teilen", SHARE_MEDIA,
                             body)
        except VendoError:
            return None
        vbid = data.get("vbid") if isinstance(data, dict) else None
        return f"https://www.bahn.de/buchung/start?vbid={vbid}" if vbid else None

    def booking_link(self, conn: Connection, trip: Trip) -> str:
        return self.share_link(conn) or search_link(conn, trip)


def _db_message(text: str) -> str | None:
    try:
        j = json.loads(text)
        msg = (j.get("details") or {}).get("anzeigeText")
        return msg.strip() if isinstance(msg, str) and msg.strip() else None
    except Exception:
        return None


def _default_transport(method: str, url: str, headers: dict, data):
    try:
        if _cc is not None:
            r = _cc.request(method, url, headers=headers, data=data,
                            timeout=TIMEOUT, impersonate="chrome")
        else:
            r = requests.request(method, url, headers=headers, data=data,
                                 timeout=TIMEOUT)
    except Exception as e:  # network down, DNS, TLS …
        raise VendoError(f"network error: {e}") from e
    return r.status_code, r.content.decode("utf-8", "replace")
