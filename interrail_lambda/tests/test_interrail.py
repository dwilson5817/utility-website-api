import pytest
import requests
from fastapi.testclient import TestClient

from interrail import app
from interrail.router import TRANSITOUS

# --- Canned upstream (Transitous / MOTIS 2) payloads -------------------------

# /api/v1/geocode results. Only `type == "STOP"` entries with an id are kept.
GEOCODE_STOPS = [
    {"type": "STOP", "id": "ch:1:sloid:7000", "name": "Bern"},
    {"type": "STOP", "id": "ch:1:sloid:7100", "name": "Bern Wankdorf"},
    {"type": "PLACE", "id": "way/123", "name": "Bern (city centre)"},  # dropped
    {"type": "STOP", "id": None, "name": "no id"},  # dropped
]

def _direct(itinerary_legs):
    return {"legs": itinerary_legs}


def _leg(*, line, trip, dep_sched, dep_actual, arr_sched, arr_actual,
         realtime=False, cancelled=False, headsign="Spiez", platform="8"):
    return {
        "mode": "LONG_DISTANCE", "routeShortName": line, "agencyName": "SBB",
        "headsign": headsign, "realTime": realtime, "cancelled": cancelled,
        "tripId": trip,
        "scheduledStartTime": dep_sched, "startTime": dep_actual,
        "scheduledEndTime": arr_sched, "endTime": arr_actual,
        "from": {"name": "Bern", "scheduledDeparture": dep_sched,
                 "departure": dep_actual, "track": platform},
        "to": {"name": "Spiez", "scheduledArrival": arr_sched,
               "arrival": arr_actual, "track": "1"},
    }


# /api/v3/plan itineraries. /departures keeps only direct ones (a single transit
# leg) and dedupes by tripId, so this mixes: two direct trains (one delayed, one
# cancelled), an *indirect* journey (must be filtered out), and a duplicate of
# the first (must be deduped).
IC61 = _leg(
    line="IC61", trip="trip-ic61", headsign="Interlaken Ost", realtime=True,
    dep_sched="2026-07-24T06:04:00Z", dep_actual="2026-07-24T06:09:00Z",
    arr_sched="2026-07-24T06:34:00Z", arr_actual="2026-07-24T06:39:00Z",
)
PLAN_ITINERARIES = [
    _direct([IC61]),
    _direct([_leg(  # on time, no realtime
        line="ICE", trip="trip-ice", platform="7",
        dep_sched="2026-07-24T07:00:00Z", dep_actual="2026-07-24T07:00:00Z",
        arr_sched="2026-07-24T07:20:00Z", arr_actual="2026-07-24T07:20:00Z",
    )]),
    {  # indirect (change at Thun) -> filtered out entirely
        "legs": [
            {"mode": "REGIONAL_RAIL", "routeShortName": "S1", "tripId": "trip-s1",
             "realTime": False, "cancelled": False,
             "scheduledStartTime": "2026-07-24T06:50:00Z",
             "startTime": "2026-07-24T06:50:00Z",
             "scheduledEndTime": "2026-07-24T07:10:00Z",
             "endTime": "2026-07-24T07:10:00Z",
             "from": {"name": "Bern"}, "to": {"name": "Thun"}},
            {"mode": "WALK", "realTime": False,
             "scheduledStartTime": "2026-07-24T07:10:00Z",
             "startTime": "2026-07-24T07:10:00Z",
             "scheduledEndTime": "2026-07-24T07:15:00Z",
             "endTime": "2026-07-24T07:15:00Z",
             "from": {"name": "Thun"}, "to": {"name": "Thun"}},
            {"mode": "REGIONAL_RAIL", "routeShortName": "RB", "tripId": "trip-rb2",
             "realTime": False, "cancelled": False,
             "scheduledStartTime": "2026-07-24T07:20:00Z",
             "startTime": "2026-07-24T07:20:00Z",
             "scheduledEndTime": "2026-07-24T07:40:00Z",
             "endTime": "2026-07-24T07:40:00Z",
             "from": {"name": "Thun"}, "to": {"name": "Spiez"}},
        ],
    },
    _direct([IC61]),  # duplicate trip -> deduped
    _direct([_leg(  # cancelled
        line="RB", trip="trip-rb", platform="3", cancelled=True,
        dep_sched="2026-07-24T08:00:00Z", dep_actual="2026-07-24T08:00:00Z",
        arr_sched="2026-07-24T08:20:00Z", arr_actual="2026-07-24T08:20:00Z",
    )]),
]


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Stand-in for requests.Session returning canned MOTIS responses."""

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/api/v1/geocode"):
            text = (params.get("text") or "").lower()
            if "boom" in text:  # simulate an upstream failure
                return FakeResponse(None, status=500)
            if "nowhere" in text:  # nothing resolves
                return FakeResponse([])
            return FakeResponse(GEOCODE_STOPS)
        if url.endswith("/api/v3/plan"):
            return FakeResponse({"itineraries": PLAN_ITINERARIES})
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def client():
    original = TRANSITOUS.session
    TRANSITOUS.session = FakeSession()
    try:
        yield TestClient(app)
    finally:
        TRANSITOUS.session = original


# --- /stations ---------------------------------------------------------------

def test_station_search_keeps_only_stops(client):
    body = client.get("/stations", params={"query": "Bern"}).json()
    assert body == [
        {"id": "ch:1:sloid:7000", "name": "Bern"},
        {"id": "ch:1:sloid:7100", "name": "Bern Wankdorf"},
    ]


def test_station_search_respects_limit(client):
    body = client.get(
        "/stations", params={"query": "Bern", "limit": 1}
    ).json()
    assert body == [{"id": "ch:1:sloid:7000", "name": "Bern"}]


def test_station_search_upstream_error_is_502(client):
    r = client.get("/stations", params={"query": "Boom"})
    assert r.status_code == 502


# --- /departures -------------------------------------------------------------

def _lines(body):
    return [d["line"] for d in body]


def test_departures_are_direct_and_deduped(client):
    body = client.get(
        "/departures", params={"from": "Bern", "to": "Spiez"}
    ).json()
    # Only direct trains, deduped by trip, soonest first: the indirect (S1/RB via
    # Thun) journey is dropped and the duplicate IC61 appears once.
    assert _lines(body) == ["IC61", "ICE", "RB"]


def test_departures_shape_and_raw_times(client):
    body = client.get(
        "/departures", params={"from": "Bern", "to": "Spiez"}
    ).json()
    ic61 = body[0]
    assert ic61["mode"] == "train"  # simplified from MOTIS "LONG_DISTANCE"
    assert ic61["operator"] == "SBB"
    assert ic61["direction"] == "Interlaken Ost"
    assert ic61["realtime"] is True
    assert ic61["cancelled"] is False
    # Raw scheduled + actual pass straight through (delay is the frontend's job).
    assert "2026-07-24T06:04:00" in ic61["departure"]["scheduled"]
    assert "2026-07-24T06:09:00" in ic61["departure"]["actual"]
    assert ic61["departure"]["platform"] == "8"
    assert "2026-07-24T06:39:00" in ic61["arrival"]["actual"]


def test_departures_no_realtime_actual_equals_scheduled(client):
    body = client.get(
        "/departures", params={"from": "Bern", "to": "Spiez"}
    ).json()
    ice = next(d for d in body if d["line"] == "ICE")
    assert ice["realtime"] is False
    assert ice["departure"]["actual"] == ice["departure"]["scheduled"]


def test_departures_cancelled(client):
    body = client.get(
        "/departures", params={"from": "Bern", "to": "Spiez"}
    ).json()
    rb = next(d for d in body if d["line"] == "RB")
    assert rb["cancelled"] is True


def test_departures_unknown_station_is_404(client):
    r = client.get("/departures", params={"from": "Nowhere", "to": "Spiez"})
    assert r.status_code == 404


def test_transport_type_mapping():
    from interrail.transitous import Transitous

    tt = Transitous._transport_type
    assert tt("HIGHSPEED_RAIL") == "train"
    assert tt("REGIONAL_RAIL") == "train"
    assert tt("BUS") == "bus"
    assert tt("TRAM") == "tram"
    assert tt("FERRY") == "ferry"
    assert tt("SOMETHING_NEW") == "something_new"  # unknown -> lower-cased
    assert tt(None) is None


# --- /manifest ---------------------------------------------------------------

def test_manifest_shape(client):
    body = client.get("/manifest").json()
    # Destinations (stays), flights (static) and legs (live boards).
    assert {item["type"] for item in body} == {"destination", "flight", "leg"}
    assert body[0]["type"] == "destination" and body[0]["name"] == "Dublin"
    assert body[-1]["type"] == "destination" and body[-1]["name"] == "Dublin"


def test_manifest_destination_is_display_only(client):
    body = client.get("/manifest").json()
    munich = next(
        i for i in body if i["type"] == "destination" and i["name"] == "Munich"
    )
    assert munich == {
        "type": "destination", "flag": "🇩🇪", "name": "Munich", "country": "Germany",
    }


def test_manifest_flight_is_static_with_details(client):
    body = client.get("/manifest").json()
    flight = next(i for i in body if i["type"] == "flight")
    assert flight == {
        "type": "flight", "start": "Dublin", "end": "Genève-Aéroport",
        "number": "EI 0680", "operator": "Aer Lingus",
        "departure_at": "2026-07-29T05:15:00Z",
    }


def test_manifest_leg_names_stations_and_mode(client):
    body = client.get("/manifest").json()
    leg = next(i for i in body if i["type"] == "leg")
    # Explicit mode + from/to (serialised as `from`) so the frontend can show the
    # icon and call /departures directly — the first hop is Genève-Aéroport->Bern.
    assert leg == {
        "type": "leg", "mode": "train", "from": "Genève-Aéroport", "to": "Bern",
    }
    # The Därligen hops are buses, flagged up-front without a /departures call.
    bus = next(i for i in body if i["type"] == "leg" and i["mode"] == "bus")
    assert bus["from"] == "Spiez, Bahnhof" and bus["to"] == "Därligen, Dorf"


def test_manifest_flight_hands_off_to_leg(client):
    body = client.get("/manifest").json()
    # A flight is immediately followed by a leg that starts at its arrival
    # airport (the flight lands, then rail carries on).
    flight = next(i for i in body if i["type"] == "flight")
    following = body[body.index(flight) + 1]
    assert following["type"] == "leg"
    assert following["from"] == flight["end"]
