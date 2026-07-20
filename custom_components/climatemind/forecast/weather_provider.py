"""Forecast provider adapter used by ForecastFeeder."""

from __future__ import annotations

from homeassistant.core import HomeAssistant


class WeatherProvider:
    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:  # type: ignore[name-defined]
        self.hass = hass
        self.coordinator = coordinator

    async def async_get_hourly_temperatures(self, hours: int = 12) -> list[float]:
        # Use coordinator's weather_manager forecast if available
        wm = getattr(self.coordinator, "_weather_manager", None)
        if wm is None:
            return []
        forecast = wm.forecast or []
        temps: list[float] = []
        for entry in forecast[:hours]:
            t = entry.get("temperature")
            if t is not None:
                temps.append(float(t))
        return temps
