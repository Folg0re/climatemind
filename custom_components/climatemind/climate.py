"""Climate platform for ClimateMind."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DOMAIN,
    OVERRIDE_CUSTOM,
    is_override_active,
)
from .coordinator import ClimateMindCoordinator


def _round_to_step(val: float | None, step: float = 0.5) -> float | None:
    """Arrotonda un valore numerico allo step desiderato (es. 0.5°C per gli AC)."""
    if val is None or not isinstance(val, (int, float)):
        return None
    return round(round(val / step) * step, 2)


def _create_room_climates(
    coordinator: ClimateMindCoordinator,
    area_id: str,
) -> list[ClimateEntity]:
    """Create climate entities for a room."""
    return [ClimateMindOverrideClimate(coordinator, area_id)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ClimateMind climate entities from a config entry."""
    coordinator: ClimateMindCoordinator = hass.data[DOMAIN][entry.entry_id]
    store = hass.data[DOMAIN]["store"]
    coordinator.async_add_climate_entities = async_add_entities
    rooms = store.get_rooms()
    entities: list[ClimateEntity] = []
    for area_id in rooms:
        entities.extend(_create_room_climates(coordinator, area_id))
        coordinator._climate_entity_areas.add(area_id)
    if entities:
        async_add_entities(entities)


class ClimateMindOverrideClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity for room override control."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-alert"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5.0
    _attr_max_temp = 35.0

    def __init__(self, coordinator: ClimateMindCoordinator, area_id: str) -> None:
        super().__init__(coordinator)
        self._area_id = area_id
        self._attr_unique_id = f"{DOMAIN}_{area_id}_override"
        self._attr_name = f"{area_id} Override"
        self.entity_id = f"climate.{DOMAIN}_{area_id}_override"

    def _room(self) -> dict | None:
        store = self.coordinator.hass.data[DOMAIN]["store"]
        room: dict | None = store.get_room(self._area_id)
        return room

    def _climate_mode(self) -> str:
        room = self._room()
        return room.get("climate_mode", "auto") if room else "auto"

    def _is_override_active(self) -> bool:
        room = self._room()
        if room is None:
            return False
        return is_override_active(room)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        base = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._climate_mode() == "auto":
            return base | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return base | ClimateEntityFeature.TARGET_TEMPERATURE

    @property
    def hvac_modes(self) -> list[HVACMode]:
        mode = self._climate_mode()
        if mode == "auto":
            return [HVACMode.OFF, HVACMode.HEAT_COOL]
        if mode == "cool_only":
            return [HVACMode.OFF, HVACMode.COOL]
        return [HVACMode.OFF, HVACMode.HEAT]

    @property
    def hvac_mode(self) -> HVACMode:
        if not self._is_override_active():
            return HVACMode.OFF
        mode = self._climate_mode()
        if mode == "auto":
            return HVACMode.HEAT_COOL
        if mode == "cool_only":
            return HVACMode.COOL
        return HVACMode.HEAT

    @property
    def target_temperature(self) -> float | None:
        if not self._is_override_active():
            return None
        room = self._room() or {}
        offset = room.get("calibration_offset", 0.0)

        if self._climate_mode() == "cool_only":
            val = room.get("override_cool")
        else:
            val = room.get("override_heat")

        if isinstance(val, (int, float)):
            return _round_to_step(float(val) + offset, self._attr_target_temperature_step)
        return None

    @property
    def target_temperature_low(self) -> float | None:
        if not self._is_override_active():
            return None
        room = self._room() or {}
        offset = room.get("calibration_offset", 0.0)
        val = room.get("override_heat")
        if isinstance(val, (int, float)):
            return _round_to_step(float(val) + offset, self._attr_target_temperature_step)
        return None

    @property
    def target_temperature_high(self) -> float | None:
        if not self._is_override_active():
            return None
        room = self._room() or {}
        offset = room.get("calibration_offset", 0.0)
        val = room.get("override_cool")
        if isinstance(val, (int, float)):
            return _round_to_step(float(val) + offset, self._attr_target_temperature_step)
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the room's current temperature from coordinator data."""
        data = self.coordinator.data
        if not data:
            return None
        room_data = data.get("rooms", {}).get(self._area_id)
        if not room_data:
            return None
        val = room_data.get("current_temp")
        return float(val) if isinstance(val, (int, float)) else None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set override targets from range or single temperature, applying inverse offset."""
        store = self.coordinator.hass.data[DOMAIN]["store"]
        room = self._room() or {}
        offset = room.get("calibration_offset", 0.0)

        mode = self._climate_mode()
        low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        high = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        single = kwargs.get(ATTR_TEMPERATURE)

        if low is not None or high is not None:
            heat = (low - offset) if low is not None else None
            cool = (high - offset) if high is not None else None
        elif single is not None:
            adjusted_single = single - offset
            if mode == "cool_only":
                heat, cool = None, adjusted_single
            elif mode == "heat_only":
                heat, cool = adjusted_single, None
            else:
                heat = adjusted_single
                cool = max(adjusted_single, room.get("comfort_cool", DEFAULT_COMFORT_COOL))
        else:
            return

        await store.async_update_room(
            self._area_id,
            {
                "override_heat": heat,
                "override_cool": cool,
                "override_until": None,
                "override_type": OVERRIDE_CUSTOM,
            },
        )
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode: OFF clears override, any other mode activates it."""
        store = self.coordinator.hass.data[DOMAIN]["store"]
        if hvac_mode == HVACMode.OFF:
            await store.async_update_room(
                self._area_id,
                {
                    "override_heat": None,
                    "override_cool": None,
                    "override_until": None,
                    "override_type": None,
                },
            )
        elif not self._is_override_active():
            room = self._room() or {}
            mode = self._climate_mode()
            heat = room.get("comfort_heat", DEFAULT_COMFORT_HEAT) if mode != "cool_only" else None
            cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL) if mode != "heat_only" else None
            await store.async_update_room(
                self._area_id,
                {
                    "override_heat": heat,
                    "override_cool": cool,
                    "override_until": None,
                    "override_type": OVERRIDE_CUSTOM,
                },
            )
        await self.coordinator.async_request_refresh()
