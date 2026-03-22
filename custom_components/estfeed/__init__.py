"""Estfeed Gas integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ElectricityPriceClient, EstfeedApiClient, GasPriceClient, OpenMeteoClient
from .const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, DOMAIN
from .coordinator import (
    ElectricityPriceCoordinator,
    EstfeedDataCoordinator,
    GasPriceCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Estfeed from a config entry."""
    session = async_get_clientsession(hass)
    estfeed_api = EstfeedApiClient(
        session,
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_CLIENT_SECRET],
    )
    weather_api = OpenMeteoClient(session)
    gas_price_api = GasPriceClient(session)
    electricity_price_api = ElectricityPriceClient(session)
    coordinator = EstfeedDataCoordinator(hass, entry, estfeed_api, weather_api)
    gas_price_coordinator = GasPriceCoordinator(hass, entry, gas_price_api)
    electricity_price_coordinator = ElectricityPriceCoordinator(
        hass, entry, electricity_price_api
    )

    # Main gas coordinator is critical — let it raise ConfigEntryNotReady
    await coordinator.async_config_entry_first_refresh()

    # Price coordinators are non-critical: if they fail on first refresh,
    # log and continue — sensors will show unavailable until next update
    # succeeds, but they won't block the main gas sensors from loading.
    for name, coord in [
        ("gas_price", gas_price_coordinator),
        ("electricity_price", electricity_price_coordinator),
    ]:
        try:
            await coord.async_config_entry_first_refresh()
        except Exception:
            _LOGGER.warning("Initial %s fetch failed; will retry on schedule", name)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "gas_price_coordinator": gas_price_coordinator,
        "electricity_price_coordinator": electricity_price_coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
