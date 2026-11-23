"""Sensors for the Sunrun integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SunrunConfigEntry
from .const import CONF_SITE_ID, DOMAIN
from .coordinator import SunrunCoordinator, SunrunData


@dataclass(frozen=True, kw_only=True)
class SunrunSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SunrunData], float | datetime | None]


SENSORS = (
    SunrunSensorDescription(
        key="energy_today",
        translation_key="energy_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        # No state class: Sunrun revises recent slots downward, which would be read as a reset
        # and double-count. The external statistic is the one for the Energy dashboard.
        suggested_display_precision=2,
        value_fn=lambda d: d.today_kwh,
    ),
    SunrunSensorDescription(
        key="last_data",
        translation_key="last_data",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d.last_data,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SunrunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SunrunSensor(coordinator, entry, desc) for desc in SENSORS)


class SunrunSensor(CoordinatorEntity[SunrunCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: SunrunSensorDescription

    def __init__(
        self,
        coordinator: SunrunCoordinator,
        entry: SunrunConfigEntry,
        description: SunrunSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        site_id = entry.data[CONF_SITE_ID]
        self._attr_unique_id = f"{site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, site_id)}, name=entry.title, manufacturer="Sunrun"
        )

    @property
    def native_value(self) -> float | datetime | None:
        return self.entity_description.value_fn(self.coordinator.data)
