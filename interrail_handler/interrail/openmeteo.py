from collections.abc import Sequence

import requests

from .cache import TTLCache
from .models import CurrentWeather, DailyForecast, DestinationWeather

#: Weather at a fixed city changes slowly, and every destination shares one
#: batched request, so a modest TTL keeps upstream calls to a handful per hour
#: regardless of how many people are viewing the trip.
WEATHER_TTL = 15 * 60


class OpenMeteo:
    """Client for the Open-Meteo forecast API.

    Open-Meteo (https://open-meteo.com) is a free, keyless weather API that
    aggregates national government models (DWD, Météo-France, ECMWF, MET Norway)
    and serves them under CC BY 4.0 — the weather analogue of the Transitous
    decision. The free tier is fair-use rate-limited per source IP; our one
    batched, cached request per refresh sits at ~1% of the daily budget.

    ``base_url`` is a class attribute so it can be repointed at a self-hosted
    instance (Open-Meteo is open-source) or a commercial key host; ``session``
    is injectable so tests can stub the transport.
    """

    base_url = "https://api.open-meteo.com"

    #: Current-conditions variables requested (order is cosmetic).
    CURRENT = ("temperature_2m", "weather_code", "wind_speed_10m")
    #: Daily-forecast variables requested.
    DAILY = (
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_probability_max",
    )
    #: Days of forecast to include (today + the next two).
    FORECAST_DAYS = 3

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        # One long-lived cache per instance; a single batched entry covers the
        # whole trip, so it stays tiny.
        self._cache = TTLCache(ttl=WEATHER_TTL, maxsize=8)

    def clear_caches(self) -> None:
        """Forget everything cached from upstream."""
        self._cache.clear()

    # -- helpers ---------------------------------------------------------------

    def _fetch(self, params: dict) -> list[dict]:
        """Fetch forecasts for ``params``, cached for :data:`WEATHER_TTL`.

        Open-Meteo returns a JSON array when several locations are requested and
        a single object for one; normalise both to a list.
        """

        def fetch() -> list[dict]:
            response = self.session.get(
                f"{self.base_url}/v1/forecast", params=params, timeout=10
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else [data]

        return self._cache.get_or_call(tuple(sorted(params.items())), fetch)

    @staticmethod
    def _daily(daily: dict) -> list[DailyForecast]:
        """Transpose Open-Meteo's column-per-variable daily block into rows."""
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        pops = daily.get("precipitation_probability_max", [])

        def at(column: list, i: int):
            return column[i] if i < len(column) else None

        return [
            DailyForecast(
                date=day,
                weather_code=at(codes, i),
                temperature_max=at(highs, i),
                temperature_min=at(lows, i),
                precipitation_probability=at(pops, i),
            )
            for i, day in enumerate(dates)
        ]

    def _destination(self, name: str, block: dict) -> DestinationWeather:
        current = block.get("current", {})
        return DestinationWeather(
            destination=name,
            latitude=block.get("latitude"),
            longitude=block.get("longitude"),
            timezone=block.get("timezone"),
            current=CurrentWeather(
                time=current.get("time"),
                temperature=current.get("temperature_2m"),
                weather_code=current.get("weather_code"),
                wind_speed=current.get("wind_speed_10m"),
            ),
            daily=self._daily(block.get("daily", {})),
        )

    # -- public API ------------------------------------------------------------

    def forecast(
        self, locations: Sequence[tuple[str, float, float]]
    ) -> list[DestinationWeather]:
        """Return current conditions + a short forecast for each location.

        ``locations`` is ``(name, latitude, longitude)`` tuples. All locations
        are fetched in a **single** batched request (comma-separated
        coordinates); Open-Meteo preserves order, so results map back to the
        input names by position.
        """
        locations = list(locations)
        if not locations:
            return []

        params = {
            "latitude": ",".join(str(lat) for _, lat, _ in locations),
            "longitude": ",".join(str(lon) for _, _, lon in locations),
            "current": ",".join(self.CURRENT),
            "daily": ",".join(self.DAILY),
            "forecast_days": self.FORECAST_DAYS,
            # Per-location local time, so `current.time` and the daily dates read
            # naturally for each destination.
            "timezone": "auto",
            # Pin units so the response is deterministic (the models document °C
            # / km/h to match).
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
        }

        blocks = self._fetch(params)
        return [
            self._destination(name, block)
            for (name, _, _), block in zip(locations, blocks)
        ]
