from datetime import date
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class DestinationItem(BaseModel):
    """A place stayed at on the trip.

    Mostly display, but carries fixed coordinates so ``/weather`` can fetch the
    forecast for each destination without a geocoding round-trip, and the IANA
    ``timezone`` so the frontend can show each destination's local time.
    """

    type: Literal["destination"] = "destination"
    flag: str
    name: str
    country: str
    latitude: float
    longitude: float
    #: IANA timezone name (e.g. ``"Europe/Berlin"``) for showing local time.
    timezone: str
    #: The day we travel on from here — one per travel day, driving the UI's
    #: per-day separators. Optional: omitted for the final home stop (Dublin),
    #: where the trip ends and there's no onward travel.
    depart: date | None = None


class FlightItem(BaseModel):
    """A flight between two places.

    Flights are static markers: they aren't in the routing engine and aren't
    covered by the Interrail pass, so — unlike ``leg`` hops — they can't be shown
    as a live departure board. The ``end`` airport is where the following ``leg``
    begins.
    """

    type: Literal["flight"] = "flight"
    start: str
    end: str
    number: str
    operator: str


class LegItem(BaseModel):
    """A single direct travel hop, shown live as a departure board.

    The route is planned in advance as a sequence of single-vehicle hops (a
    change of train is two legs, not one). The frontend renders each leg by
    calling ``/departures?from={from}&to={to}`` — the next direct trains/buses
    from ``from`` that call at ``to`` (rail-preferred, bus fallback). The
    manifest never pins a service; the board shows every direct option.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["leg"] = "leg"
    #: Planned transport type, so the frontend can show the icon without first
    #: querying ``/departures``. Matches the ``mode`` on a live ``Departure``.
    mode: Literal["train", "bus"] = "train"
    #: Origin station (serialised as ``from``; ``from`` is a Python keyword).
    from_: str = Field(alias="from")
    to: str


# Discriminated union so each item validates against the right schema by `type`.
ManifestItem = Annotated[
    Union[DestinationItem, FlightItem, LegItem], Field(discriminator="type")
]


# Hardcoded manifest. This is the single source of truth for the trip and is the
# seam a database will later sit behind (see `get_manifest`).
#
# The trip is an ordered sequence of destinations (places stayed), flights
# (static) and legs (single direct hops shown as live departure boards). Each
# `leg` is one vehicle; where the pre-planned route changes trains, that's two
# legs. Station names are chosen to geocode cleanly against MOTIS; Därligen has
# no train station, so it uses the bus stop "Därligen, Dorf".
_MANIFEST: list[ManifestItem] = [
    DestinationItem(
        flag="🇮🇪", name="Dublin", country="Ireland",
        latitude=53.3498, longitude=-6.2603, timezone="Europe/Dublin",
        depart="2026-07-29",
    ),
    FlightItem(
        start="Dublin", end="Genève-Aéroport",
        number="EI 0680", operator="Aer Lingus",
    ),
    LegItem(from_="Genève-Aéroport", to="Bern"),
    LegItem(from_="Bern", to="Spiez"),
    LegItem(mode="bus", from_="Spiez, Bahnhof", to="Därligen, Dorf"),
    DestinationItem(
        flag="🇨🇭", name="Därligen", country="Switzerland",
        latitude=46.6635, longitude=7.8497, timezone="Europe/Zurich",
        depart="2026-08-01",
    ),
    LegItem(mode="bus", from_="Därligen, Dorf", to="Spiez, Bahnhof"),
    LegItem(from_="Spiez", to="Bern"),
    LegItem(from_="Bern", to="Zürich HB"),
    LegItem(from_="Zürich HB", to="München Hbf"),
    DestinationItem(
        flag="🇩🇪", name="Munich", country="Germany",
        latitude=48.1374, longitude=11.5755, timezone="Europe/Berlin",
        depart="2026-08-03",
    ),
    LegItem(from_="München Hbf", to="Praha hlavní nádraží"),
    DestinationItem(
        flag="🇨🇿", name="Prague", country="Czech Republic",
        latitude=50.0755, longitude=14.4378, timezone="Europe/Prague",
        depart="2026-08-05",
    ),
    LegItem(from_="Praha hlavní nádraží", to="Berlin Hbf"),
    DestinationItem(
        flag="🇩🇪", name="Berlin", country="Germany",
        latitude=52.5200, longitude=13.4050, timezone="Europe/Berlin",
        depart="2026-08-08",
    ),
    FlightItem(
        start="Berlin", end="Dublin",
        number="EI 0337", operator="Aer Lingus",
    ),
    DestinationItem(
        flag="🇮🇪", name="Dublin", country="Ireland",
        latitude=53.3498, longitude=-6.2603, timezone="Europe/Dublin",
    ),
]


def get_manifest() -> list[ManifestItem]:
    """Return the trip manifest.

    Currently returns the hardcoded manifest; this is the single seam to swap
    for a database-backed source later.
    """
    return _MANIFEST
