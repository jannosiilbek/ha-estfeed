"""Diagnostics support for PV24 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    coord_data = coordinator.data or {}

    return {
        "has_gas": coord_data.get("has_gas"),
        "area_ratio": coord_data.get("area_ratio"),
        "is_estimated": coord_data.get("is_estimated"),
        "gas": coord_data.get("gas"),
        "metering_points": (
            len(coordinator.metering_points) if coordinator.metering_points else 0
        ),
        "last_update_success": coordinator.last_update_success,
    }
