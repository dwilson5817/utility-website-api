from datetime import datetime

import requests
from fastapi import APIRouter, HTTPException, Query, Response

from .manifest import DestinationItem, ManifestItem, get_manifest
from .models import Departure, DestinationWeather, Station, StationNotFound
from .openmeteo import OpenMeteo
from .transitous import Transitous

# No path prefix: the API layer maps /interrail to this function (main.py strips
# it via Mangum), so the app serves bare paths (/manifest, /stations, ...).
router = APIRouter(tags=["interrail"])

# Single source of truth for all live train/bus data (see transitous.py).
TRANSITOUS = Transitous()
# Weather for the destinations (see openmeteo.py).
WEATHER = OpenMeteo()

# How long clients may reuse a response without asking again. This is the second
# half of easing load on Transitous: the caches in transitous.py stop repeat
# requests reaching upstream, and these stop them reaching us at all. Kept short
# for departures, which change as delays come in.
_MANIFEST_MAX_AGE = 300
_STATIONS_MAX_AGE = 3600
_DEPARTURES_MAX_AGE = 15
_WEATHER_MAX_AGE = 600


@router.get("/manifest", response_model_exclude_none=True)
def read_manifest(response: Response) -> list[ManifestItem]:
    """Return the trip manifest: the ordered destinations and travel legs."""
    response.headers["Cache-Control"] = f"public, max-age={_MANIFEST_MAX_AGE}"
    return get_manifest()


@router.get("/stations")
def search_stations(response: Response, query: str, limit: int = 10) -> list[Station]:
    """Resolve a free-text ``query`` to matching stations for autocomplete."""
    try:
        stations = TRANSITOUS.search_stations(query, limit)
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Upstream routing API error"
        ) from exc

    response.headers["Cache-Control"] = f"public, max-age={_STATIONS_MAX_AGE}"
    return stations


@router.get("/departures")
def get_departures(
    response: Response,
    origin: str = Query(..., alias="from"),
    destination: str = Query(..., alias="to"),
    when: datetime | None = None,
    modes: str | None = None,
    limit: int = 8,
) -> list[Departure]:
    """Return the next direct departures for one pre-planned hop, soonest first.

    A board of direct trains/buses leaving ``from`` that call at ``to`` — not a
    journey planner. By default it prefers trains (which an Interrail pass
    covers) and only falls back to allowing buses when no direct train exists
    (e.g. a last mile). Pass ``modes`` explicitly to override (e.g. ``BUS``).

    :param from: Origin — a station id (from ``/stations``) or a free-text name.
    :param to: Next stop the train must call at — a station id or a name.
    :param when: Depart-after time (ISO-8601); defaults to now.
    :param modes: Transit modes to allow, e.g. ``RAIL`` or ``BUS``. Omit for the
        rail-preferred, bus-fallback default.
    :param limit: Maximum number of departures to return.
    """
    try:
        if modes is None:
            departures = TRANSITOUS.departures(origin, destination, when=when, modes="RAIL", limit=limit)
            if not departures:  # no direct train — allow buses to fill the gap
                departures = TRANSITOUS.departures(
                    origin, destination, when=when, modes="RAIL,BUS", limit=limit
                )
        else:
            departures = TRANSITOUS.departures(origin, destination, when=when, modes=modes, limit=limit)
    except StationNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Unknown station '{exc}'") from None
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Upstream routing API error"
        ) from exc

    response.headers["Cache-Control"] = f"public, max-age={_DEPARTURES_MAX_AGE}"
    return departures


@router.get("/weather")
def get_weather(response: Response) -> list[DestinationWeather]:
    """Current conditions and a short forecast for each trip destination.

    The destinations (and their coordinates) come from the manifest, so this
    takes no parameters. All destinations are fetched in one batched, cached
    upstream call; the result is keyed by destination name for the frontend to
    join onto the manifest.
    """
    # Distinct destinations in trip order (Dublin appears at both ends).
    seen: set[str] = set()
    locations: list[tuple[str, float, float]] = []
    for item in get_manifest():
        if isinstance(item, DestinationItem) and item.name not in seen:
            seen.add(item.name)
            locations.append((item.name, item.latitude, item.longitude))

    try:
        weather = WEATHER.forecast(locations)
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Upstream weather API error"
        ) from exc

    response.headers["Cache-Control"] = f"public, max-age={_WEATHER_MAX_AGE}"
    return weather
