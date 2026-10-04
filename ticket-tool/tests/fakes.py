"""Offline fixtures in the /mob response shapes (see api-tests/healthcheck.py)."""
import json
from datetime import datetime, timedelta

from bbtickets.vendo import BERLIN, VendoClient

BERLIN_ID = "A=1@O=Berlin Hbf@X=13369549@Y=52525589@U=80@L=8011160@"
MUENCHEN_ID = "A=1@O=München Hbf@X=11558339@Y=48140229@U=80@L=8000261@"


def leg(train, frm, to, dep, arr, walk=False, delay=0):
    a = {
        "abgangsOrt": {"name": frm}, "ankunftsOrt": {"name": to},
        "abgangsDatum": dep.isoformat(), "ankunftsDatum": arr.isoformat(),
    }
    if walk:
        a["typ"] = "FUSSWEG"
    else:
        a["mitteltext"] = train
        a["kurztext"] = train.split()[0]
        if delay:
            a["ezAbgangsDatum"] = (dep + timedelta(minutes=delay)).isoformat()
    return a


def conn(dep, trains=("ICE 597",), price=29.99, minutes=240):
    """A connection departing `dep` riding `trains` back to back."""
    legs, t = [], dep
    step = timedelta(minutes=minutes // len(trains))
    stops = ["Berlin Hbf"] + [f"Hub {i}" for i in range(1, len(trains))] + ["München Hbf"]
    for i, tr in enumerate(trains):
        legs.append(leg(tr, stops[i], stops[i + 1], t, t + step))
        t += step
    c = {"verbindung": {"verbindungsAbschnitte": legs,
                        "kontext": f"¶HKI¶T$A=1@{dep:%H%M}$¶"}}
    if price is not None:
        c["angebote"] = {"preise": {"gesamt": {"ab": {"betrag": price,
                                                      "waehrung": "EUR"}}}}
    return c


def day_at(day: str, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=BERLIN)


class FakeDB:
    """Routes /mob calls to canned answers; records what was asked."""

    def __init__(self):
        self.calls = []
        self.fahrplan_pages = [[]]   # list of pages (lists of conns)
        self.statuses = []           # queued status codes to return first
        self.vbid = "11111111-2222-3333-4444-555555555555"

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data else None
        path = url.split("/mob/", 1)[1]
        self.calls.append((method, path, body))
        if self.statuses:
            return self.statuses.pop(0), '{"details":{"anzeigeText":"kaputt"}}'
        if path == "location/search":
            term = body["searchTerm"]
            return 200, json.dumps([
                {"name": "Adresse " + term, "locationId": "A=2@O=x", "locationType": "ADR"},
                {"name": term + " Hbf", "locationId": f"A=1@O={term} Hbf@L=1@",
                 "evaNr": "1", "locationType": "ST"},
            ])
        if path == "angebote/fahrplan":
            ctx = body["reiseHin"]["wunsch"].get("context")
            idx = int(ctx) if ctx else 0
            page = self.fahrplan_pages[idx] if idx < len(self.fahrplan_pages) else []
            nxt = str(idx + 1) if idx + 1 < len(self.fahrplan_pages) else None
            return 200, json.dumps({"verbindungen": page, "spaeterContext": nxt})
        if path == "angebote/verbindung/teilen":
            return 201, json.dumps({"vbid": self.vbid})
        if path == "angebote/tagesbestpreis":
            return 200, json.dumps({"tagesbestPreisIntervalle": []})
        return 404, "{}"


def client(fake=None):
    fake = fake or FakeDB()
    return VendoClient(min_interval=0, sleep=lambda s: None, transport=fake), fake
