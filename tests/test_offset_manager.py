import pytest

from custom_components.climatemind.calibration.offset_manager import OffsetManager


class DummyStore:
    def __init__(self) -> None:
        self._data: dict[str, float] = {"sensor.a": 1.5}
        self.saved_offsets = None

    async def async_load(self) -> None:
        return None

    async def async_save(self) -> None:
        self.saved_offsets = dict(self._data)

    def get_offsets(self) -> dict[str, float]:
        return dict(self._data)

    def set_offset(self, entity_id: str, offset: float) -> None:
        self._data[entity_id] = float(offset)


@pytest.mark.asyncio
async def test_offset_manager_loads_and_persists_offsets() -> None:
    store = DummyStore()
    manager = OffsetManager(hass=object(), store=store)

    await manager.async_load()
    assert manager._offsets == {"sensor.a": 1.5}

    await manager.async_set_offset("sensor.b", 0.25)

    assert manager._offsets["sensor.b"] == 0.25
    assert store.saved_offsets == {"sensor.a": 1.5, "sensor.b": 0.25}
