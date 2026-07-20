"""Inject forecast horizon into MPC and other consumers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .weather_provider import WeatherProvider


class ForecastFeeder:
    def __init__(self, hass: HomeAssistant, coordinator: Any) -> None:  # type: ignore[name-defined]
        self.hass = hass
        self.coordinator = coordinator
        self._provider = WeatherProvider(hass, coordinator)

    async def async_get_temperature_horizon(self, hours: int = 6) -> list[float]:
        temps = await self._provider.async_get_hourly_temperatures(hours)
        # If not enough entries, pad with current outdoor temp if available
        if len(temps) < hours:
            cur = None
            wm = getattr(self.coordinator, "_weather_manager", None)
            if wm:
                # try to read current outdoor temp from forecast first entry
                if wm.forecast:
                    cur = wm.forecast[0].get("temperature")
            if cur is None:
                cur = 0.0
            temps += [float(cur)] * (hours - len(temps))
        return temps[:hours]
