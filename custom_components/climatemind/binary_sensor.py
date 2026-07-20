"""Binary sensor platform for ClimateMind."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ClimateMindCoordinator


def _create_room_binary_sensors(
    coordinator: ClimateMindCoordinator,
    area_id: str,
) -> list[BinarySensorEntity]:
    """Create binary sensor entities for a room."""
    return [ClimateMindCoverPausedSensor(coordinator, area_id)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ClimateMind binary sensor entities from a config entry."""
    coordinator: ClimateMindCoordinator = hass.data[DOMAIN][entry.entry_id]
    store = hass.data[DOMAIN]["store"]
    coordinator.async_add_binary_sensor_entities = async_add_entities
    rooms = store.get_rooms()
    entities: list[BinarySensorEntity] = []
    for area_id, room in rooms.items():
        if room.get("covers"):
            entities.extend(_create_room_binary_sensors(coordinator, area_id))
            coordinator._binary_sensor_entity_areas.add(area_id)
    # Global central heating availability sensor
    entities.append(ClimateMindCentralHeatAvailableSensor(coordinator))
    if entities:
        async_add_entities(entities)


class ClimateMindCoverPausedSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating if cover auto-control is paused by user override."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ClimateMindCoordinator, area_id: str) -> None:
        super().__init__(coordinator)
        self._area_id = area_id
        self._attr_unique_id = f"{DOMAIN}_{area_id}_cover_paused"
        self._attr_name = f"{area_id} Cover Paused"
        self._attr_icon = "mdi:hand-back-right"
        self.entity_id = f"binary_sensor.{DOMAIN}_{area_id}_cover_paused"

    @property
    def is_on(self) -> bool:
        """Return True if user override is active (auto-control paused)."""
        if self.coordinator.data is None:
            return False
        room = self.coordinator.data.get("rooms", {}).get(self._area_id)
        return bool(room.get("cover_auto_paused", False)) if room else False


class ClimateMindCentralHeatAvailableSensor(CoordinatorEntity, BinarySensorEntity):
    """Global binary sensor exposing whether central heat is available now."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ClimateMindCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_central_heat_available_now"
        self._attr_name = "Calore disponibile ora"
        self._attr_icon = "mdi:radiator"
        self.entity_id = f"binary_sensor.{DOMAIN}_central_heat_available_now"

    @property
    def is_on(self) -> bool:
        """Return True when central heating manager reports availability."""
        manager = getattr(self.coordinator, "central_heating", None)
        if manager is None:
            manager = self.hass.data.get(DOMAIN, {}).get("central_heating")
        if manager is None:
            return False
        return bool(manager.is_heat_available_now())
