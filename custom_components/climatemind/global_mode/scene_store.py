"""Persistent scene storage."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_scenes"


class SceneStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        self._data = stored or {}

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    def get_scene(self, scene_id: str) -> dict | None:
        return self._data.get(scene_id)

    def list_scenes(self) -> list[str]:
        return list(self._data.keys())

    def save_scene(self, scene_id: str, data: dict) -> None:
        self._data[scene_id] = data

    def delete_scene(self, scene_id: str) -> None:
        self._data.pop(scene_id, None)
