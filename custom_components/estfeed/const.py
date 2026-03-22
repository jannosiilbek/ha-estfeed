"""Constants for the Estfeed integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

DOMAIN = "estfeed"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_APARTMENT_AREA = "apartment_area_m2"
CONF_BUILDING_AREA = "building_area_m2"

DEFAULT_UPDATE_INTERVAL = 3600  # 1 hour
GAS_PRICE_UPDATE_INTERVAL = 3600  # 1 hour — price changes daily

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAS_PRICE_URL = "https://dashboard.elering.ee/api/gas-trade"
ELECTRICITY_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
ELECTRICITY_PRICE_UPDATE_INTERVAL = 900  # 15 minutes — matches price block resolution

API_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
PRICE_API_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"

# Thermal inertia weights — validated over 196 days (Sep 2025–Mar 2026).
# Building thermal mass means gas usage lags temperature by 1-2 days.
# 3-day model (MAPE 8.1%, MAE 3.0 m³) outperforms 2-day (8.6%, 3.2 m³).
THERMAL_WEIGHTS = (0.40, 0.40, 0.20)  # today, yesterday, day-before

MIN_COMPLETE_DAY_HOURS = 20  # Skip days with fewer hours of data
DEFAULT_CALORIFIC_KWH_M3 = 10.6  # Estonian natural gas typical value

# Tallinn default coordinates (used by test scripts; HA uses home location)
DEFAULT_LAT = 59.437
DEFAULT_LON = 24.7536


def get_area_config(entry: ConfigEntry) -> tuple[float, float]:
    """Get apartment and building area from config entry (options take precedence)."""
    apartment = entry.options.get(
        CONF_APARTMENT_AREA, entry.data.get(CONF_APARTMENT_AREA, 0)
    )
    building = entry.options.get(
        CONF_BUILDING_AREA, entry.data.get(CONF_BUILDING_AREA, 0)
    )
    return apartment, building
