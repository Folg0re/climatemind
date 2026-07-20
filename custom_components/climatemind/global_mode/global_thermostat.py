"""Global thermostat manager (whole-home mode)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant


class GlobalModeManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._mode: str | None = None
        self._setpoint: float | None = None

    async def async_set_mode(self, mode: str) -> None:
        self._mode = mode

    def get_mode(self) -> str | None:
        return self._mode

    async def async_set_setpoint(self, temp: float) -> None:
        self._setpoint = float(temp)

    def get_setpoint(self) -> float | None:
        return self._setpoint
