"""Diagnostic sensor state tests."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.kef_lsx.models import CommunicationHealth, RuntimeSnapshot
from custom_components.kef_lsx.sensor import DESCRIPTIONS, KefLsxSensor


def test_status_sensor_exposes_degraded_separately_from_availability() -> None:
    coordinator = SimpleNamespace(
        data=RuntimeSnapshot(health=CommunicationHealth(degraded=True, available=True))
    )
    sensor = object.__new__(KefLsxSensor)
    sensor.coordinator = coordinator
    sensor.entity_description = DESCRIPTIONS[0]
    assert sensor.native_value == "degraded"
