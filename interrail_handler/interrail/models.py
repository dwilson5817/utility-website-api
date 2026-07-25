from datetime import datetime

from pydantic import BaseModel


class StationNotFound(Exception):
    """Raised when a free-text place does not resolve to a real station."""


class Station(BaseModel):
    """A resolvable station, for autocomplete."""

    id: str
    name: str


class StopPoint(BaseModel):
    """One end of a departure: a stop with timetabled and real-time times.

    Both times are passed straight through from upstream. When there is no live
    data ``actual`` equals ``scheduled`` (see :attr:`Departure.realtime`); the
    delay, if any, is ``actual - scheduled`` and is left for the caller to
    compute.
    """

    name: str
    #: Timetabled time.
    scheduled: datetime
    #: Real-time time; equals ``scheduled`` when no live data is available.
    actual: datetime
    #: Platform / track, when known.
    platform: str | None = None


class Departure(BaseModel):
    """A single direct train/bus leaving one stop and calling at another.

    This is one row of a departure board for a pre-planned hop: board it at
    ``departure`` and it takes you directly to ``arrival``.
    """

    #: Kind of transport, simplified to e.g. ``"train"``, ``"bus"``, ``"tram"``.
    mode: str | None = None
    #: Line/service name, e.g. ``"IC61"``.
    line: str | None = None
    #: Operating company, e.g. ``"Schweizerische Bundesbahnen SBB"``.
    operator: str | None = None
    #: Head sign — the train's final destination, e.g. ``"Interlaken Ost"``.
    direction: str | None = None
    #: Whether the times reflect live data (else timetable).
    realtime: bool = False
    cancelled: bool = False
    #: Boarding stop (the hop's origin).
    departure: StopPoint
    #: Where it drops you (the hop's next stop).
    arrival: StopPoint
