"""Inietta le serie temporali meteo nell'orizzonte di predizione MPC."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .weather_provider import WeatherProvider

_LOGGER = logging.getLogger(__name__)


class ForecastFeeder:
    """Prepara il vettore di temperatura esterna per il nucleo MPC."""

    def __init__(self, hass: HomeAssistant, weather_provider: WeatherProvider) -> None:
        self.hass = hass
        self._weather_provider = weather_provider

    async def async_get_temperature_horizon(self, horizon_hours: int = 6) -> list[float]:
        """Restituisce la serie di temperature esterne per le prossime N ore."""
        current_temp = getattr(self._weather_provider, "get_current_temperature", lambda: None)()
        forecast_method = getattr(self._weather_provider, "async_get_hourly_forecast", None)
        forecast_series: list[float] = []

        if forecast_method is not None:
            forecast_series = await forecast_method(horizon_hours)

        fallback_val = current_temp if current_temp is not None else 20.0

        if not forecast_series:
            _LOGGER.debug("Forecast meteo non disponibile. Fallback su temperatura istantanea.")
            return [fallback_val] * horizon_hours

        # Garanzia di lunghezza: se il forecast è più corto dell'orizzonte, lo completiamo
        # con l'ultimo valore valido; se è più lungo, lo troncamos.
        if len(forecast_series) < horizon_hours:
            last_val = forecast_series[-1] if forecast_series else fallback_val
            forecast_series.extend([last_val] * (horizon_hours - len(forecast_series)))
        elif len(forecast_series) > horizon_hours:
            forecast_series = forecast_series[:horizon_hours]

        return forecast_series

    async def async_get_outdoor_series(self, horizon_hours: int = 6) -> list[float]:
        """Compatibilità con il vecchio nome del metodo."""
        return await self.async_get_temperature_horizon(horizon_hours)
