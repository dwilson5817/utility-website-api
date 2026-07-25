from datetime import datetime

import requests
from fastapi import APIRouter, HTTPException, Query

from .manifest import ManifestItem, get_manifest
from .models import Departure, Station, StationNotFound
from .transitous import Transitous

# No path prefix: the API layer maps /interrail to this function (main.py strips
# it via Mangum), so the app serves bare paths (/manifest, /stations, ...).
router = APIRouter(tags=["interrail"])

# Single source of truth for all live train/bus data (see transitous.py).
TRANSITOUS = Transitous()


@router.get("/manifest", response_model_exclude_none=True)
def read_manifest() -> list[ManifestItem]:
    """Return the trip manifest: the ordered destinations and travel legs."""
    return get_manifest()


@router.get("/stations")
def search_stations(query: str, limit: int = 10) -> list[Station]:
    """Resolve a free-text ``query`` to matching stations for autocomplete."""
    try:
        return TRANSITOUS.search_stations(query, limit)
    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="Upstream routing API error"
        ) from exc


@router.get("/departures")
def get_departures(
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

    return departures
