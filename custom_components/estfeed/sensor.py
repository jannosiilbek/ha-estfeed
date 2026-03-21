"""Sensor entities for Estfeed gas integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EstfeedDataCoordinator


@dataclass(kw_only=True)
class EstfeedSensorDescription(SensorEntityDescription):
    """Describes an Estfeed sensor entity."""

    value_fn: Callable[[dict[str, Any]], float | bool | None]


SENSORS: tuple[EstfeedSensorDescription, ...] = (
    EstfeedSensorDescription(
        key="apartment_gas_total",
        translation_key="apartment_gas_total",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_total_m3"],
    ),
    EstfeedSensorDescription(
        key="apartment_gas_energy_total",
        translation_key="apartment_gas_energy_total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_total_kwh"],
    ),
    EstfeedSensorDescription(
        key="apartment_gas_today",
        translation_key="apartment_gas_today",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_today_m3"],
    ),
    EstfeedSensorDescription(
        key="apartment_gas_flow_rate",
        translation_key="apartment_gas_flow_rate",
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
        device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data["gas"]["apartment_flow_rate_m3h"],
    ),
    EstfeedSensorDescription(
        key="apartment_gas_estimated",
        translation_key="apartment_gas_estimated",
        value_fn=lambda data: data["is_estimated"],
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Estfeed sensors from a config entry."""
    coordinator: EstfeedDataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EstfeedSensor(coordinator, entry, desc) for desc in SENSORS
    )


class EstfeedSensor(CoordinatorEntity[EstfeedDataCoordinator], SensorEntity):
    """Representation of an Estfeed gas sensor."""

    entity_description: EstfeedSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EstfeedDataCoordinator,
        entry: ConfigEntry,
        description: EstfeedSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_apartment")},
            "name": "Apartment Gas",
            "manufacturer": "Elering",
            "model": "Estfeed",
            "entry_type": DeviceEntryType.SERVICE,
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available or self.coordinator.data is None:
            return False
        return self.coordinator.data.get("has_gas", False)

    @property
    def native_value(self) -> float | bool | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None
