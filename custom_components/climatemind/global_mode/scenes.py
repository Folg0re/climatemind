"""Scene engine for whole-home presets."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .scene_store import SceneStore


class SceneManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = SceneStore(hass)

    async def async_load(self) -> None:
        await self._store.async_load()

    def save_scene(self, scene_id: str, data: dict) -> None:
        self._store.save_scene(scene_id, data)

    async def async_apply_scene(self, scene_id: str) -> None:
        scene = self._store.get_scene(scene_id)
        if not scene:
            return
        # Minimal: apply per-room presets by calling services or writing to store
        for area_id, preset in scene.get("rooms", {}).items():
            # example: set offsets or set room target via services
            await self.hass.services.async_call(
                "climatemind", "set_offset", {"area_id": area_id, "offset": preset.get("offset", 0.0)}
            )
        # handle global mode activation
        if scene.get("global_mode"):
            await self.hass.services.async_call("climatemind", "set_global_mode", {"mode": scene.get("global_mode")})
