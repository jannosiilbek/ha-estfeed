"""Sensor entities for Estfeed integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EstfeedDataCoordinator


@dataclass(kw_only=True)
class EstfeedSensorDescription(SensorEntityDescription):
    """Describes an Estfeed sensor entity."""

    value_fn: Callable[[dict[str, Any]], Optional[float]]
    available_fn: Callable[[dict[str, Any]], bool] = lambda data: True
    device_name: str = "Building Energy"
    device_id_suffix: str = "building"


# Building sensors
BUILDING_SENSORS: tuple[EstfeedSensorDescription, ...] = (
    EstfeedSensorDescription(
        key="building_electricity_daily",
        translation_key="building_electricity_daily",
        name="Building Electricity Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["electricity"]["building_daily_kwh"],
        available_fn=lambda data: data["has_electricity"],
    ),
    EstfeedSensorDescription(
        key="building_electricity_monthly",
        translation_key="building_electricity_monthly",
        name="Building Electricity This Month",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["electricity"]["building_monthly_kwh"],
        available_fn=lambda data: data["has_electricity"],
    ),
    EstfeedSensorDescription(
        key="building_gas_daily",
        translation_key="building_gas_daily",
        name="Building Gas Today",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["gas"]["building_daily_m3"],
        available_fn=lambda data: data["has_gas"],
        icon="mdi:fire",
    ),
    EstfeedSensorDescription(
        key="building_gas_monthly",
        translation_key="building_gas_monthly",
        name="Building Gas This Month",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["gas"]["building_monthly_m3"],
        available_fn=lambda data: data["has_gas"],
        icon="mdi:fire",
    ),
    EstfeedSensorDescription(
        key="building_gas_energy_daily",
        translation_key="building_gas_energy_daily",
        name="Building Gas Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["gas"]["building_daily_kwh"],
        available_fn=lambda data: data["has_gas"],
    ),
    EstfeedSensorDescription(
        key="building_gas_energy_monthly",
        translation_key="building_gas_energy_monthly",
        name="Building Gas Energy This Month",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
        value_fn=lambda data: data["gas"]["building_monthly_kwh"],
        available_fn=lambda data: data["has_gas"],
    ),
)

# Apartment sensors
APARTMENT_SENSORS: tuple[EstfeedSensorDescription, ...] = (
    EstfeedSensorDescription(
        key="apartment_gas_daily",
        translation_key="apartment_gas_daily",
        name="Apartment Gas Today",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_daily_m3"],
        available_fn=lambda data: data["has_gas"] and data["area_ratio"] > 0,
        device_name="Apartment Energy",
        device_id_suffix="apartment",
        icon="mdi:fire",
    ),
    EstfeedSensorDescription(
        key="apartment_gas_monthly",
        translation_key="apartment_gas_monthly",
        name="Apartment Gas This Month",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.GAS,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_monthly_m3"],
        available_fn=lambda data: data["has_gas"] and data["area_ratio"] > 0,
        device_name="Apartment Energy",
        device_id_suffix="apartment",
        icon="mdi:fire",
    ),
    EstfeedSensorDescription(
        key="apartment_gas_energy_daily",
        translation_key="apartment_gas_energy_daily",
        name="Apartment Gas Energy Today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_daily_kwh"],
        available_fn=lambda data: data["has_gas"] and data["area_ratio"] > 0,
        device_name="Apartment Energy",
        device_id_suffix="apartment",
    ),
    EstfeedSensorDescription(
        key="apartment_gas_energy_monthly",
        translation_key="apartment_gas_energy_monthly",
        name="Apartment Gas Energy This Month",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_monthly_kwh"],
        available_fn=lambda data: data["has_gas"] and data["area_ratio"] > 0,
        device_name="Apartment Energy",
        device_id_suffix="apartment",
    ),
    EstfeedSensorDescription(
        key="electricity_spot_price",
        translation_key="electricity_spot_price",
        name="Electricity Spot Price",
        native_unit_of_measurement="EUR/kWh",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["price"]["electricity_spot_eur_kwh"],
        device_name="Apartment Energy",
        device_id_suffix="apartment",
        icon="mdi:currency-eur",
    ),
    EstfeedSensorDescription(
        key="electricity_avg_price",
        translation_key="electricity_avg_price",
        name="Electricity Avg Price This Month",
        native_unit_of_measurement="EUR/kWh",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["price"]["electricity_avg_eur_kwh"],
        device_name="Apartment Energy",
        device_id_suffix="apartment",
        icon="mdi:currency-eur",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Estfeed sensors from a config entry."""
    coordinator: EstfeedDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[EstfeedSensor] = []

    for description in BUILDING_SENSORS:
        entities.append(EstfeedSensor(coordinator, entry, description))

    for description in APARTMENT_SENSORS:
        entities.append(EstfeedSensor(coordinator, entry, description))

    async_add_entities(entities)


class EstfeedSensor(CoordinatorEntity[EstfeedDataCoordinator], SensorEntity):
    """Representation of an Estfeed sensor."""

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
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{description.device_id_suffix}")},
            "name": description.device_name,
            "manufacturer": "Elering",
            "model": "Estfeed",
            "entry_type": DeviceEntryType.SERVICE,
        }

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self.coordinator.data is None:
            return False
        return self.entity_description.available_fn(self.coordinator.data)

    @property
    def native_value(self) -> Optional[float]:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None
