"""Service facade for ClimateMind integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from ..const import DOMAIN


async def async_register_services(
    hass: HomeAssistant, store: Any, offset_manager: Any, scene_manager: Any, global_mode: Any
) -> None:
    """Register ClimateMind services exposed to Home Assistant."""

    async def _set_offset(call):
        data = call.data
        area = data.get("area_id")
        offset = float(data.get("offset", 0.0))
        duration = data.get("duration")
        if area == "all":
            # apply to all known rooms
            rooms = store.get_rooms()
            for rid in rooms.keys():
                await offset_manager.async_set_offset(rid, offset, duration)
        else:
            await offset_manager.async_set_offset(area, offset, duration)

    async def _apply_scene(call):
        scene_id = call.data.get("scene_id")
        if not scene_id:
            return
        await scene_manager.async_apply_scene(scene_id)

    async def _set_global_mode(call):
        mode = call.data.get("mode")
        if not mode:
            return
        await global_mode.async_set_mode(mode)

    hass.services.async_register(DOMAIN, "set_offset", _set_offset)
    hass.services.async_register(DOMAIN, "apply_scene", _apply_scene)
    hass.services.async_register(DOMAIN, "set_global_mode", _set_global_mode)
