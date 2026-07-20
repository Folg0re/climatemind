"""Persistent storage for sensor offsets."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_offsets"


class OffsetStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        self._data = stored or {}

    async def async_save(self) -> None:
        await self._store.async_save(self._data)

    def get_offsets(self) -> dict:
        return dict(self._data)

    def get_offset(self, entity_id: str) -> float | None:
        return self._data.get(entity_id)

    def set_offset(self, entity_id: str, offset: float) -> None:
        self._data[entity_id] = float(offset)

    def clear_offset(self, entity_id: str) -> None:
        self._data.pop(entity_id, None)
