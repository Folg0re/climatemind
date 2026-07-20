from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from ..heating_source.central_calendar import CentralCalendar
from ..managers.weather_manager import WeatherManager


class CentralHeatingManager:
    """Manager che espone disponibilità di calore usando calendario+forecast.

    - `is_heat_available_now()` ritorna True se il calendario segnala impianto
      acceso ora o se la serie di forecast contiene un flag `heat_available`.
    - `async_forecast_availability(horizon_hours)` ritorna una lista di bool per
      ogni ora dell'orizzonte basata sui dati meteo (campo `heat_available`) e
      sul calendario come fallback.
    """

    def __init__(self, hass: HomeAssistant, weather_mgr: WeatherManager, calendar: CentralCalendar) -> None:
        self.hass = hass
        self._weather_mgr = weather_mgr
        self._calendar = calendar

    def is_heat_available_now(self, at: datetime | None = None) -> bool:
        now = at or datetime.now()
        # Check calendar first
        if self._calendar.is_on(now):
            return True

        # Then check latest forecast entry (hour 0) for explicit availability
        forecast = self._weather_mgr.forecast
        if forecast:
            first = forecast[0]
            avail = first.get("heat_available")
            if isinstance(avail, bool):
                return avail
        return False

    async def async_forecast_availability(self, horizon_hours: int = 6) -> list[bool]:
        """Return a list of booleans for each forecast hour in the horizon.

        Preference order per hour: forecast[hour]['heat_available'] -> calendar
        for that hour.
        """
        result: list[bool] = []
        forecast = self._weather_mgr.forecast
        # Use forecast entries when available
        for hr in range(horizon_hours):
            avail = False
            if hr < len(forecast):
                entry = forecast[hr]
                val = entry.get("heat_available")
                if isinstance(val, bool):
                    avail = val
                    result.append(avail)
                    continue

            # Fallback to calendar for that hour
            check_time = datetime.now() + timedelta(hours=hr)
            avail = self._calendar.is_on(check_time)
            result.append(avail)

        return result
