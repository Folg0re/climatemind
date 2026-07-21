"""Forecast provider adapter used by ForecastFeeder."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant


class WeatherProvider:
    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:
        self.hass = hass
        self.coordinator = coordinator

    def get_current_temperature(self) -> float | None:
        """Restituisce la temperatura corrente, se disponibile nel coordinatore."""
        if self.coordinator is None:
            return None
        wm = getattr(self.coordinator, "_weather_manager", None)
        if wm is None:
            return None
        forecast = getattr(wm, "forecast", None) or []
        if not forecast:
            return None
        first = forecast[0]
        temperature = first.get("temperature")
        return float(temperature) if temperature is not None else None

    async def async_get_hourly_forecast(self, hours: int = 12) -> list[float]:
        """Restituisce la serie oraria di temperature dal weather manager."""
        if self.coordinator is None:
            return []
        wm = getattr(self.coordinator, "_weather_manager", None)
        if wm is None:
            return []
        forecast = getattr(wm, "forecast", None) or []
        temps: list[float] = []
        for entry in forecast[:hours]:
            t = entry.get("temperature")
            if t is not None:
                temps.append(float(t))
        return temps

    async def async_get_hourly_temperatures(self, hours: int = 12) -> list[float]:
        """Alias per compatibilità con il vecchio nome."""
        return await self.async_get_hourly_forecast(hours)
