"""Gestore calibrazione offset sensori temperatura."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .offset_store import OffsetStore

_LOGGER = logging.getLogger(__name__)


class OffsetManager:
    """Gestisce la mappa degli offset e applica la calibrazione alle letture grezze."""

    def __init__(self, hass: HomeAssistant, store: OffsetStore) -> None:
        self.hass = hass
        self._store = store
        self._offsets: dict[str, float] = {}

    async def async_load(self) -> None:
        """Carica gli offset salvati dallo Storage di HA."""
        await self._store.async_load()
        self._offsets = {k: float(v) for k, v in self._store.get_offsets().items()}

    def get_calibrated_temperature(self, entity_id: str, raw_temp: float | None) -> float | None:
        """Applica l'offset prima dell'elaborazione EKF."""
        if raw_temp is None:
            return None
        offset = self._offsets.get(entity_id, 0.0)
        return round(raw_temp + offset, 2)

    async def async_set_offset(self, entity_id: str, offset: float) -> None:
        """Aggiorna e persiste un offset per una determinata entità."""
        self._offsets[entity_id] = float(offset)
        self._store.set_offset(entity_id, float(offset))
        await self._store.async_save()
        _LOGGER.info("Offset aggiornato per %s: %.2f°C", entity_id, offset)
