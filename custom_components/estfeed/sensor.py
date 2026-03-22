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
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfVolume, UnitOfVolumeFlowRate
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN
from .coordinator import (
    ElectricityPriceCoordinator,
    EstfeedDataCoordinator,
    GasPriceCoordinator,
)


@dataclass(kw_only=True)
class EstfeedSensorDescription(SensorEntityDescription):
    """Describes an Estfeed sensor entity."""

    value_fn: Callable[[dict[str, Any]], float | bool | None]
    attr_fn: Callable[[dict[str, Any]], dict[str, Any] | None] = lambda _: None


ELECTRICITY_PRICE_SENSORS: tuple[EstfeedSensorDescription, ...] = (
    EstfeedSensorDescription(
        key="electricity_market_price",
        translation_key="electricity_market_price",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["current_price_eur_kwh"],
        attr_fn=lambda data: {
            "prices_today": data["prices_today"],
            "prices_tomorrow": data["prices_tomorrow"],
        },
    ),
    EstfeedSensorDescription(
        key="electricity_price_today_avg",
        translation_key="electricity_price_today_avg",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["today_avg_eur_kwh"],
    ),
    EstfeedSensorDescription(
        key="electricity_price_today_min",
        translation_key="electricity_price_today_min",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["today_min_eur_kwh"],
    ),
    EstfeedSensorDescription(
        key="electricity_price_today_max",
        translation_key="electricity_price_today_max",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["today_max_eur_kwh"],
    ),
    EstfeedSensorDescription(
        key="electricity_price_next_hour",
        translation_key="electricity_price_next_hour",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["next_hour_eur_kwh"],
    ),
)

GAS_PRICE_SENSORS: tuple[EstfeedSensorDescription, ...] = (
    EstfeedSensorDescription(
        key="gas_market_price",
        translation_key="gas_market_price",
        native_unit_of_measurement="EUR/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda data: data["price_eur_kwh"],
        attr_fn=lambda data: {"price_date": data["price_date"]},
    ),
)

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
        key="apartment_gas_energy_today",
        translation_key="apartment_gas_energy_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda data: data["gas"]["apartment_today_kwh"],
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
        key="apartment_gas_power",
        translation_key="apartment_gas_power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data["gas"]["apartment_power_kw"],
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
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EstfeedDataCoordinator = data["coordinator"]
    gas_price_coordinator: GasPriceCoordinator = data["gas_price_coordinator"]
    elec_price_coordinator: ElectricityPriceCoordinator = data["electricity_price_coordinator"]

    entities: list[SensorEntity] = [
        EstfeedSensor(coordinator, entry, desc) for desc in SENSORS
    ]
    entities.extend(
        PriceSensor(
            gas_price_coordinator, entry, desc,
            device_id_suffix="gas_price",
            device_name="Gas Market Price",
            device_model="GET Baltic",
        )
        for desc in GAS_PRICE_SENSORS
    )
    entities.extend(
        PriceSensor(
            elec_price_coordinator, entry, desc,
            device_id_suffix="electricity_price",
            device_name="Electricity Market Price",
            device_model="Nord Pool EE",
        )
        for desc in ELECTRICITY_PRICE_SENSORS
    )
    async_add_entities(entities)


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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.attr_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None


class PriceSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """Representation of a market price sensor (gas or electricity)."""

    entity_description: EstfeedSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry: ConfigEntry,
        description: EstfeedSensorDescription,
        *,
        device_id_suffix: str,
        device_name: str,
        device_model: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{entry.entry_id}_{device_id_suffix}")},
            "name": device_name,
            "manufacturer": "Elering",
            "model": device_model,
            "entry_type": DeviceEntryType.SERVICE,
        }

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.value_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.coordinator.data is None:
            return None
        try:
            return self.entity_description.attr_fn(self.coordinator.data)
        except (KeyError, TypeError):
            return None
