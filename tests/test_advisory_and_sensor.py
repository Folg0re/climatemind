"""Unit tests for advisory engine and central heat binary sensor."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.climatemind.advisory.advisory_engine import AdvisoryEngine
from custom_components.climatemind.binary_sensor import ClimateMindCentralHeatAvailableSensor


def test_advisory_respects_central_heating():
    # central heating unavailable -> no recommendation
    fake_ch = SimpleNamespace(is_heat_available_now=lambda: False)
    engine = AdvisoryEngine(max_position=5, central_heating=fake_ch)
    assert engine.recommend_position(0.8) == 0

    # central heating available -> scaled recommendation
    fake_ch2 = SimpleNamespace(is_heat_available_now=lambda: True)
    engine2 = AdvisoryEngine(max_position=5, central_heating=fake_ch2)
    assert engine2.recommend_position(0.8) > 0


def test_binary_sensor_reads_central_heating():
    # Dummy coordinator with central_heating manager
    fake_ch = SimpleNamespace(is_heat_available_now=lambda: True)
    coordinator = SimpleNamespace(central_heating=fake_ch)
    sensor = ClimateMindCentralHeatAvailableSensor(coordinator)
    assert sensor.is_on is True
