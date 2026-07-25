from datetime import datetime

import requests

from .cache import TTLCache
from .models import (
    Departure,
    Station,
    StationNotFound,
    StopPoint,
)

#: Station ids are effectively permanent, and geocoding is the call we make most
#: (every departures lookup resolves two free-text names), so hold results for a
#: long time — in practice the container dies long before this expires.
GEOCODE_TTL = 24 * 60 * 60

#: Departure boards carry real-time delays and cancellations, so they may only
#: be reused briefly. Repeat views of the same leg within the window are free.
PLAN_TTL = 30


# MOTIS reports a granular mode per leg; we collapse it to a simple transport
# type (the fine-grained sub-type is already carried by the line name, e.g.
# "ICE"/"IC"/"RE"). Unknown modes fall through as their lower-cased name.
_TRANSPORT_TYPES = {
    "HIGHSPEED_RAIL": "train",
    "LONG_DISTANCE": "train",
    "NIGHT_RAIL": "train",
    "REGIONAL_FAST_RAIL": "train",
    "REGIONAL_RAIL": "train",
    "METRO": "subway",
    "SUBWAY": "subway",
    "TRAM": "tram",
    "BUS": "bus",
    "COACH": "bus",
    "FERRY": "ferry",
}


class Transitous:
    """Client for the Transitous / MOTIS 2 routing API.

    Transitous (https://transitous.org) is a community-run, non-commercial
    routing backend. It aggregates national open-transit feeds (including the
    official ``opentransportdata.swiss`` and German DELFI data) and exposes them
    through the MOTIS 2 API, so a single instance covers the whole trip with
    real-time data.

    ``base_url`` is a class attribute so it can be repointed at a self-hosted
    MOTIS instance; ``session`` is injectable so tests can stub the transport.
    """

    base_url = "https://api.transitous.org"

    #: Transitous' fair-use policy rejects generic user-agents, so identify the
    #: app (with a contact) per https://transitous.org/api/.
    user_agent = "utility-website-interrail (dwilson5817@gmail.com)"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        # Overwrite the default UA on a real requests.Session (requests
        # pre-populates one, so setdefault would be a no-op); injected fakes
        # have no headers.
        if isinstance(self.session, requests.Session):
            self.session.headers["User-Agent"] = self.user_agent

        # Caches are per-instance so tests (and any future second client) start
        # empty; the app holds one long-lived instance, so in production these
        # live for as long as the Lambda container does.
        self._geocode_cache = TTLCache(ttl=GEOCODE_TTL, maxsize=256)
        self._plan_cache = TTLCache(ttl=PLAN_TTL, maxsize=64)

    def clear_caches(self) -> None:
        """Forget everything cached from upstream."""
        self._geocode_cache.clear()
        self._plan_cache.clear()

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        """Parse an ISO-8601 timestamp (``…Z`` or offset) to a datetime."""
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _resolve(self, place: str) -> str:
        """Resolve a place to a MOTIS stop id.

        A MOTIS stop id always contains a ``:`` (e.g. ``de-DELFI_ch:1:sloid:…``)
        and is passed straight through; anything else is treated as free text
        and geocoded to the best-matching station.
        """
        if ":" in place:
            return place
        matches = self.search_stations(place, limit=1)
        if not matches:
            raise StationNotFound(place)
        return matches[0].id

    def _stop(
        self,
        place: dict,
        fallback_scheduled: str | None,
        fallback_actual: str | None,
        *,
        arrival: bool,
    ) -> StopPoint:
        if arrival:
            scheduled = place.get("scheduledArrival") or fallback_scheduled
            actual = place.get("arrival") or fallback_actual
        else:
            scheduled = place.get("scheduledDeparture") or fallback_scheduled
            actual = place.get("departure") or fallback_actual

        return StopPoint(
            name=place.get("name"),
            scheduled=self._parse(scheduled),
            actual=self._parse(actual or scheduled),
            platform=place.get("track"),
        )

    @staticmethod
    def _transport_type(mode: str | None) -> str | None:
        if not mode:
            return None
        return _TRANSPORT_TYPES.get(mode, mode.lower())

    def _departure(self, leg: dict) -> Departure:
        return Departure(
            mode=self._transport_type(leg.get("mode")),
            line=leg.get("routeShortName"),
            operator=leg.get("agencyName"),
            direction=leg.get("headsign"),
            realtime=bool(leg.get("realTime")),
            cancelled=bool(leg.get("cancelled")),
            departure=self._stop(
                leg.get("from", {}),
                leg.get("scheduledStartTime"),
                leg.get("startTime"),
                arrival=False,
            ),
            arrival=self._stop(
                leg.get("to", {}),
                leg.get("scheduledEndTime"),
                leg.get("endTime"),
                arrival=True,
            ),
        )

    def _plan(self, params: dict) -> list[dict]:
        """Fetch itineraries for ``params``, cached for :data:`PLAN_TTL`.

        Keyed on the full parameter set, so the rail-only and rail-plus-bus
        attempts a single request can make are cached separately.
        """

        def fetch() -> list[dict]:
            response = self.session.get(
                f"{self.base_url}/api/v3/plan", params=params, timeout=20
            )
            response.raise_for_status()
            return response.json().get("itineraries", [])

        return self._plan_cache.get_or_call(tuple(sorted(params.items())), fetch)

    # -- public API ------------------------------------------------------------

    def _geocode(self, query: str) -> list[Station]:
        """Geocode ``query`` to stations, caching the full (unlimited) result.

        Caching before the limit is applied means an autocomplete asking for ten
        matches and :meth:`_resolve` asking for one share a single upstream call.
        """

        def fetch() -> list[Station]:
            response = self.session.get(
                f"{self.base_url}/api/v1/geocode",
                params={"text": query},
                timeout=10,
            )
            response.raise_for_status()
            results = response.json()
            return [
                Station(id=r["id"], name=r["name"])
                for r in results
                if r.get("type") == "STOP" and r.get("id") and r.get("name")
            ]

        # Case and surrounding space don't change what the geocoder matches, so
        # normalise them away to get more hits out of the same entry.
        return self._geocode_cache.get_or_call(query.strip().casefold(), fetch)

    def search_stations(self, query: str, limit: int = 10) -> list[Station]:
        """Resolve free-text ``query`` to matching stations for autocomplete."""
        return self._geocode(query)[:limit]

    def departures(
        self,
        origin: str,
        destination: str,
        when: datetime | None = None,
        modes: str = "RAIL",
        limit: int = 8,
    ) -> list[Departure]:
        """Return the next *direct* departures from ``origin`` that call at
        ``destination``, soonest first.

        Implemented over MOTIS's ``/plan``: we ask for several itineraries and
        keep only the direct ones (a single transit leg), which gives a clean
        departure board without inspecting each trip's stops. This deliberately
        does not do journey planning — it's one board for one pre-planned hop.

        ``origin`` / ``destination`` may be MOTIS stop ids or free-text station
        names (resolved via :meth:`search_stations`).

        :raises StationNotFound: if either endpoint cannot be resolved.
        """
        params = {
            "fromPlace": self._resolve(origin),
            "toPlace": self._resolve(destination),
            "transitModes": modes,
            # Over-fetch: some itineraries involve a change and are discarded.
            "numItineraries": max(limit * 2, 10),
        }
        if when is not None:
            params["time"] = when.isoformat()

        itineraries = self._plan(params)

        departures: list[Departure] = []
        seen: set[str] = set()
        for itinerary in itineraries:
            transit = [
                leg for leg in itinerary.get("legs", []) if leg.get("mode") != "WALK"
            ]
            if len(transit) != 1:  # not a direct connection — skip
                continue
            leg = transit[0]
            trip = leg.get("tripId")
            if trip in seen:
                continue
            seen.add(trip)
            departures.append(self._departure(leg))

        departures.sort(key=lambda d: d.departure.actual or d.departure.scheduled)
        return departures[:limit]
