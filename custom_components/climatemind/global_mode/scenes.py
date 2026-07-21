"""Motore di gestione e applicazione delle scene d'ambiente."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .scene_store import SceneStore

_LOGGER = logging.getLogger(__name__)


class SceneEngine:
    """Carica, salva ed applica configurazioni d'ambiente (scene)."""

    def __init__(self, hass: HomeAssistant, store: SceneStore) -> None:
        self.hass = hass
        self._store = store
        self._scenes: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Carica le scene salvate."""
        await self._store.async_load()
        self._scenes = {
            scene_id: scene_data for scene_id, scene_data in self._store._data.items() if isinstance(scene_data, dict)
        }

    async def async_apply_scene(self, scene_id: str) -> None:
        """Applica i preset previsti dalla scena per ciascuna area/stanza."""
        scene = self._scenes.get(scene_id)
        if not scene:
            _LOGGER.error("Scena '%s' non trovata.", scene_id)
            return

        _LOGGER.info("Applicazione scena ClimateMind: %s", scene_id)
        room_configs = scene.get("rooms", {})
        climatemind_data = self.hass.data.get("climatemind", {})
        coordinator = climatemind_data.get("coordinator")

        if coordinator is None:
            _LOGGER.warning("Coordinatore ClimateMind non disponibile, scena %s non applicata", scene_id)
            return

        for area_id, config in room_configs.items():
            preset = config.get("preset")
            target_temp = config.get("target_temp")
            await coordinator.async_apply_room_preset(area_id, preset, target_temp)

        if "global_mode" in scene:
            global_thermostat = climatemind_data.get("global_thermostat")
            if global_thermostat is not None:
                await global_thermostat.async_set_mode(scene["global_mode"])
