"""Integrazione ClimateMind per Home Assistant."""

from __future__ import annotations

import logging
import os

from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .api.services import async_setup_services
from .calibration.offset_manager import OffsetManager
from .calibration.offset_store import OffsetStore
from .const import DOMAIN
from .coordinator import ClimateMindCoordinator as ClimateMindDataUpdateCoordinator
from .forecast.forecast_feeder import ForecastFeeder
from .forecast.weather_provider import WeatherProvider
from .global_mode.scene_store import SceneStore
from .global_mode.scenes import SceneEngine
from .store import ClimateMindStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["climate", "sensor", "binary_sensor"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Setup globale del componente (YAML non utilizzato, gestito via Config Flow)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Inizializzazione dell'entry point da configurazione HACS/UI."""
    hass.data.setdefault(DOMAIN, {})

    store = ClimateMindStore(hass)
    await store.async_load()
    hass.data[DOMAIN]["store"] = store

    offset_store = OffsetStore(hass)
    offset_manager = OffsetManager(hass, offset_store)
    await offset_manager.async_load()

    scene_store = SceneStore(hass)
    scene_engine = SceneEngine(hass, scene_store)
    await scene_engine.async_load()

    weather_entity = entry.options.get("weather_entity") or entry.data.get("weather_entity")
    weather_provider = WeatherProvider(hass, weather_entity)
    forecast_feeder = ForecastFeeder(hass, weather_provider)

    coordinator = ClimateMindDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator
    hass.data[DOMAIN]["coordinator"] = coordinator
    hass.data[DOMAIN]["store"] = store
    hass.data[DOMAIN]["offset_manager"] = offset_manager
    hass.data[DOMAIN]["scene_engine"] = scene_engine
    hass.data[DOMAIN]["forecast_feeder"] = forecast_feeder

    # 1. Registriamo la cartella frontend come percorso statico HTTP
    frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path="/climatemind_static",
                path=frontend_path,
                cache_headers=False,
            )
        ]
    )

    try:
        async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="ClimateMind",
            sidebar_icon="mdi:home-thermometer",
            frontend_url_path="climatemind",
            config={
                "_panel_custom": {
                    "name": "climatemind-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    # 2. Puntiamo al nuovo URL statico locale anziché a Ingress
                    "js_url": "/climatemind_static/climatemind-panel.js",
                }
            },
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Panel ClimateMind already registered or unavailable: %s", err)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Scaricamento pulito dell'entry point."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN].get(entry.entry_id) and "coordinator" in hass.data[DOMAIN]:
            hass.data[DOMAIN].pop("coordinator", None)
    return unload_ok
