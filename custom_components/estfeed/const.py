"""Constants for the Estfeed integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

DOMAIN = "estfeed"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_APARTMENT_AREA = "apartment_area_m2"
CONF_BUILDING_AREA = "building_area_m2"

DEFAULT_UPDATE_INTERVAL = 3600  # 1 hour

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAS_PRICE_URL = "https://dashboard.elering.ee/api/gas-trade"

API_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def get_area_config(entry: ConfigEntry) -> tuple[float, float]:
    """Get apartment and building area from config entry (options take precedence)."""
    apartment = entry.options.get(
        CONF_APARTMENT_AREA, entry.data.get(CONF_APARTMENT_AREA, 0)
    )
    building = entry.options.get(
        CONF_BUILDING_AREA, entry.data.get(CONF_BUILDING_AREA, 0)
    )
    return apartment, building
