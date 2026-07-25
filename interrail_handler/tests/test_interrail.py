import pytest
import requests
from fastapi.testclient import TestClient

from interrail import app
from interrail.router import TRANSITOUS, WEATHER

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


# --- Canned upstream (Open-Meteo) payload ------------------------------------

# One block per distinct destination, in trip order: Dublin, Därligen, Munich,
# Prague, Berlin. Open-Meteo returns a JSON *array* for a multi-location request
# and each block is column-per-variable (dates parallel to the value lists).
def _weather_block(*, lat, lon, tz, temp, code, wind, is_day=1):
    return {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "current": {
            "time": "2026-07-25T14:00", "temperature_2m": temp,
            "weather_code": code, "wind_speed_10m": wind, "is_day": is_day,
        },
        "daily": {
            "time": ["2026-07-25", "2026-07-26", "2026-07-27"],
            "weather_code": [code, 61, 80],
            "temperature_2m_max": [temp + 2, temp + 1, temp + 3],
            "temperature_2m_min": [temp - 6, temp - 5, temp - 4],
            "precipitation_probability_max": [20, 70, 55],
        },
    }


WEATHER_FORECAST = [
    _weather_block(lat=53.35, lon=-6.26, tz="Europe/Dublin", temp=17.3, code=3, wind=12.5),
    _weather_block(lat=46.66, lon=7.85, tz="Europe/Zurich", temp=21.0, code=1, wind=6.0),
    _weather_block(lat=48.14, lon=11.58, tz="Europe/Berlin", temp=24.4, code=2, wind=8.1),
    _weather_block(lat=50.08, lon=14.44, tz="Europe/Prague", temp=23.1, code=0, wind=9.3),
    _weather_block(lat=52.52, lon=13.40, tz="Europe/Berlin", temp=25.0, code=95, wind=14.7, is_day=0),
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

    def __init__(self):
        #: Every request made, so tests can assert what reached upstream.
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        if url.endswith("/api/v1/geocode"):
            text = (params.get("text") or "").lower()
            if "boom" in text:  # simulate an upstream failure
                return FakeResponse(None, status=500)
            if "nowhere" in text:  # nothing resolves
                return FakeResponse([])
            return FakeResponse(GEOCODE_STOPS)
        if url.endswith("/api/v3/plan"):
            return FakeResponse({"itineraries": PLAN_ITINERARIES})
        if url.endswith("/v1/forecast"):
            return FakeResponse(WEATHER_FORECAST)
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def session():
    """Swap the shared client's transport for a fake, with empty caches.

    The app holds one long-lived Transitous, so its caches outlive a test unless
    cleared — both before (so canned data isn't served from a previous test) and
    after (so a cached fake response can't leak into a later one).
    """
    original = TRANSITOUS.session
    fake = FakeSession()
    TRANSITOUS.session = fake
    TRANSITOUS.clear_caches()
    try:
        yield fake
    finally:
        TRANSITOUS.session = original
        TRANSITOUS.clear_caches()


@pytest.fixture
def weather():
    """Swap the shared weather client's transport for a fake, caches cleared.

    Mirrors the ``session`` fixture but for the separate Open-Meteo client.
    """
    original = WEATHER.session
    fake = FakeSession()
    WEATHER.session = fake
    WEATHER.clear_caches()
    try:
        yield fake
    finally:
        WEATHER.session = original
        WEATHER.clear_caches()


@pytest.fixture
def client(session):
    return TestClient(app)


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
        "latitude": 48.1374, "longitude": 11.5755, "timezone": "Europe/Berlin",
        "depart": "2026-08-03",
    }


def test_manifest_destination_depart_dates(client):
    body = client.get("/manifest").json()
    dests = [i for i in body if i["type"] == "destination"]

    def named(name):
        return [d for d in dests if d["name"] == name]

    # Each destination's `depart` is the day we travel on from it — one per
    # travel day, driving the UI separators.
    assert named("Därligen")[0]["depart"] == "2026-08-01"
    assert named("Munich")[0]["depart"] == "2026-08-03"
    assert named("Prague")[0]["depart"] == "2026-08-05"
    assert named("Berlin")[0]["depart"] == "2026-08-08"
    # Dublin bookends the trip: we depart on the outbound day, but the final
    # home stop has no onward travel, so its `depart` is omitted (not null).
    dublins = named("Dublin")
    assert dublins[0]["depart"] == "2026-07-29"
    assert "depart" not in dublins[-1]


def test_manifest_flight_is_static_with_details(client):
    body = client.get("/manifest").json()
    flight = next(i for i in body if i["type"] == "flight")
    assert flight == {
        "type": "flight", "start": "Dublin", "end": "Genève-Aéroport",
        "number": "EI 0680", "operator": "Aer Lingus",
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


# --- /weather ----------------------------------------------------------------

def _forecasts(session):
    return [(url, p) for url, p in session.calls if url.endswith("/v1/forecast")]


def test_weather_covers_distinct_destinations_in_order(client, weather):
    body = client.get("/weather").json()
    # Dublin is a destination at both ends of the trip but weather lists it once.
    assert [w["destination"] for w in body] == [
        "Dublin", "Därligen", "Munich", "Prague", "Berlin",
    ]


def test_weather_current_and_daily_passthrough(client, weather):
    body = client.get("/weather").json()
    dublin = body[0]
    assert dublin["timezone"] == "Europe/Dublin"
    assert dublin["current"]["temperature"] == 17.3
    assert dublin["current"]["weather_code"] == 3  # raw WMO code, not translated
    assert dublin["current"]["wind_speed"] == 12.5
    # is_day (1/0 upstream) surfaces as a bool for day/night icon selection.
    assert dublin["current"]["is_day"] is True
    berlin = next(w for w in body if w["destination"] == "Berlin")
    assert berlin["current"]["is_day"] is False
    # Three days of forecast, dates and codes passed straight through.
    assert len(dublin["daily"]) == 3
    assert dublin["daily"][0]["date"] == "2026-07-25"
    assert dublin["daily"][1]["precipitation_probability"] == 70


def test_weather_is_one_batched_upstream_call(client, weather):
    client.get("/weather")
    calls = _forecasts(weather)
    # All five destinations arrive in a single request...
    assert len(calls) == 1
    _, params = calls[0]
    assert len(params["latitude"].split(",")) == 5
    assert len(params["longitude"].split(",")) == 5


def test_weather_is_cached(client, weather):
    client.get("/weather")
    client.get("/weather")
    # The second view is served from cache — upstream is hit once.
    assert len(_forecasts(weather)) == 1


def test_weather_upstream_error_is_502(client):
    class Boom:
        def get(self, *args, **kwargs):
            return FakeResponse(None, status=500)

    original = WEATHER.session
    WEATHER.session = Boom()
    WEATHER.clear_caches()
    try:
        assert client.get("/weather").status_code == 502
    finally:
        WEATHER.session = original
        WEATHER.clear_caches()


# --- caching -----------------------------------------------------------------

def _geocodes(session):
    return [p.get("text") for url, p in session.calls if url.endswith("geocode")]


def test_repeat_station_search_hits_upstream_once(client, session):
    for _ in range(3):
        client.get("/stations", params={"query": "Bern"})
    assert _geocodes(session) == ["Bern"]


def test_station_search_cache_ignores_case_and_padding(client, session):
    client.get("/stations", params={"query": "Bern"})
    client.get("/stations", params={"query": " bern "})
    assert len(_geocodes(session)) == 1


def test_station_search_caches_before_limit_is_applied(client, session):
    # A wide autocomplete and a narrow one share the cached geocode; only the
    # slice differs.
    wide = client.get("/stations", params={"query": "Bern"}).json()
    narrow = client.get("/stations", params={"query": "Bern", "limit": 1}).json()
    assert len(_geocodes(session)) == 1
    assert narrow == wide[:1]


def test_departures_reuse_cached_station_lookups(client, session):
    client.get("/departures", params={"from": "Bern", "to": "Spiez"})
    first = len(_geocodes(session))
    client.get("/departures", params={"from": "Bern", "to": "Spiez"})
    # Both endpoints resolve once and stay resolved; the repeat adds nothing.
    assert first == 2
    assert len(_geocodes(session)) == 2


def test_departures_are_cached_per_query(client, session):
    def plans():
        return [p for url, p in session.calls if url.endswith("plan")]

    client.get("/departures", params={"from": "Bern", "to": "Spiez"})
    client.get("/departures", params={"from": "Bern", "to": "Spiez"})
    assert len(plans()) == 1
    # A different leg is a different key, so it still reaches upstream. Use a
    # distinct station id (passed straight through) — free-text names all geocode
    # to the same first stub stop, which would collapse onto the cached key.
    client.get("/departures", params={"from": "Bern", "to": "ch:1:sloid:7100"})
    assert len(plans()) == 2


def test_upstream_failure_is_not_cached(client, session):
    assert client.get("/stations", params={"query": "boom"}).status_code == 502
    assert client.get("/stations", params={"query": "boom"}).status_code == 502
    # Both attempts reached upstream: a failure must not be pinned for the TTL.
    assert _geocodes(session) == ["boom", "boom"]


def test_responses_carry_cache_control(client, weather):
    assert "max-age" in client.get("/manifest").headers["cache-control"]
    assert "max-age" in client.get(
        "/stations", params={"query": "Bern"}
    ).headers["cache-control"]
    assert "max-age" in client.get(
        "/departures", params={"from": "Bern", "to": "Spiez"}
    ).headers["cache-control"]
    assert "max-age" in client.get("/weather").headers["cache-control"]
