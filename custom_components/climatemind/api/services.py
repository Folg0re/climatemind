"""Facciata di registrazione e gestione dei servizi ClimateMind per Home Assistant."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_OFFSET = "set_offset"
SERVICE_APPLY_SCENE = "apply_scene"
SERVICE_SET_GLOBAL_MODE = "set_global_mode"

SCHEMA_SET_OFFSET = vol.Schema({
    vol.Required("target_entity"): cv.entity_id,
    vol.Required("offset"): vol.Coerce(float),
})

SCHEMA_APPLY_SCENE = vol.Schema({
    vol.Required("scene_id"): cv.string,
})

SCHEMA_SET_GLOBAL_MODE = vol.Schema({
    vol.Required("mode"): vol.In(["per_room", "global_only"]),
})

async def async_setup_services(hass: HomeAssistant) -> None:
    """Registra i servizi dell'integrazione."""

    async def handle_set_offset(call: ServiceCall) -> None:
        entity_id = call.data["target_entity"]
        offset = call.data["offset"]
        _LOGGER.debug("Esecuzione service set_offset: %s -> %s°C", entity_id, offset)
        offset_mgr = hass.data[DOMAIN]["offset_manager"]
        await offset_mgr.async_set_offset(entity_id, offset)

    async def handle_apply_scene(call: ServiceCall) -> None:
        scene_id = call.data["scene_id"]
        _LOGGER.debug("Esecuzione service apply_scene: %s", scene_id)
        scene_engine = hass.data[DOMAIN]["scene_engine"]
        await scene_engine.async_apply_scene(scene_id)

    async def handle_set_global_mode(call: ServiceCall) -> None:
        mode = call.data["mode"]
        _LOGGER.debug("Esecuzione service set_global_mode: %s", mode)
        global_thermostat = hass.data[DOMAIN]["global_thermostat"]
        await global_thermostat.async_set_mode(mode)

    hass.services.async_register(DOMAIN, SERVICE_SET_OFFSET, handle_set_offset, schema=SCHEMA_SET_OFFSET)
    hass.services.async_register(DOMAIN, SERVICE_APPLY_SCENE, handle_apply_scene, schema=SCHEMA_APPLY_SCENE)
    hass.services.async_register(DOMAIN, SERVICE_SET_GLOBAL_MODE, handle_set_global_mode, schema=SCHEMA_SET_GLOBAL_MODE)
