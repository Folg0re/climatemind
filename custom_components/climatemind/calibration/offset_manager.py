"""Offset manager: apply and compute sensor offsets."""

from __future__ import annotations

import asyncio
import statistics

from homeassistant.core import HomeAssistant

from .offset_store import OffsetStore


class OffsetManager:
    """Manage sensor offsets for calibration pre-processing."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = OffsetStore(hass)
        self._auto_samples: dict[str, list[float]] = {}

    async def async_load(self) -> None:
        await self._store.async_load()

    def get_offset(self, entity_id: str) -> float | None:
        return self._store.get_offset(entity_id)

    async def async_set_offset(self, entity_id: str, offset: float, duration: int | None = None) -> None:
        self._store.set_offset(entity_id, float(offset))
        await self._store.async_save()
        if duration and duration > 0:
            # schedule clearing after duration minutes
            async def _clear():
                await asyncio.sleep(duration * 60)
                self._store.clear_offset(entity_id)
                await self._store.async_save()

            self.hass.loop.create_task(_clear())

    def apply_offset(self, raw_temp: float, entity_id: str) -> float:
        off = self.get_offset(entity_id)
        return raw_temp + (off or 0.0)

    def record_auto_sample(self, entity_id: str, reference: float, sample: float) -> None:
        # simple rolling collection for auto-calibration
        diff = reference - sample
        self._auto_samples.setdefault(entity_id, []).append(diff)
        # cap samples
        if len(self._auto_samples[entity_id]) > 500:
            self._auto_samples[entity_id].pop(0)

    def compute_auto_offset(self, entity_id: str) -> float | None:
        s = self._auto_samples.get(entity_id)
        if not s:
            return None
        return float(statistics.mean(s))
