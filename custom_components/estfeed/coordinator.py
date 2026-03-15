"""DataUpdateCoordinator for Estfeed integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EleringPriceClient,
    EstfeedApiClient,
    EstfeedApiError,
    EstfeedAuthError,
)
from .const import CONF_APARTMENT_AREA, CONF_BUILDING_AREA, DEFAULT_UPDATE_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class EstfeedDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Estfeed metering data and Elering prices."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        estfeed_api: EstfeedApiClient,
        price_api: EleringPriceClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self.estfeed_api = estfeed_api
        self.price_api = price_api
        self.metering_points: list[dict[str, Any]] = []

    async def _async_setup(self) -> None:
        """Fetch metering points on first setup."""
        now = datetime.now(timezone.utc)
        try:
            self.metering_points = await self.estfeed_api.get_metering_points(
                start=now - timedelta(days=30),
                end=now,
            )
        except EstfeedAuthError as err:
            raise ConfigEntryAuthFailed from err
        except EstfeedApiError as err:
            raise UpdateFailed(f"Failed to fetch metering points: {err}") from err

        _LOGGER.info(
            "Estfeed: found %d metering point(s): %s",
            len(self.metering_points),
            ", ".join(
                f"{mp['eic']} ({mp['commodityType']})" for mp in self.metering_points
            ),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch metering data and prices."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        eics = [mp["eic"] for mp in self.metering_points]
        electricity_eics = [
            mp["eic"] for mp in self.metering_points if mp["commodityType"] == "ELECTRICITY"
        ]
        gas_eics = [
            mp["eic"] for mp in self.metering_points if mp["commodityType"] == "NATURAL_GAS"
        ]

        # Fetch metering data for the current month (daily resolution)
        try:
            metering_data = await self.estfeed_api.get_metering_data(
                start=month_start,
                end=now,
                resolution="one_day",
                eics=eics,
            )
        except EstfeedAuthError as err:
            raise ConfigEntryAuthFailed from err
        except EstfeedApiError as err:
            raise UpdateFailed(f"Failed to fetch metering data: {err}") from err

        # Parse metering data
        today_str = now.strftime("%Y-%m-%d")
        electricity = {"daily_kwh": 0.0, "monthly_kwh": 0.0}
        gas = {"daily_m3": 0.0, "monthly_m3": 0.0, "daily_kwh": 0.0, "monthly_kwh": 0.0}

        for mp_data in metering_data:
            eic = mp_data.get("meteringPointEic", "")
            if mp_data.get("error"):
                _LOGGER.warning("Error for EIC %s: %s", eic, mp_data["error"])
                continue

            is_electricity = eic in electricity_eics
            is_gas = eic in gas_eics

            for interval in mp_data.get("accountingIntervals", []):
                period_start = interval.get("periodStart", "")
                is_today = period_start.startswith(today_str) if period_start else False

                if is_electricity:
                    consumption_kwh = interval.get("consumptionKwh") or 0.0
                    electricity["monthly_kwh"] += consumption_kwh
                    if is_today:
                        electricity["daily_kwh"] += consumption_kwh

                if is_gas:
                    consumption_m3 = interval.get("consumptionM3") or 0.0
                    consumption_kwh = interval.get("consumptionKwh") or 0.0
                    gas["monthly_m3"] += consumption_m3
                    gas["monthly_kwh"] += consumption_kwh
                    if is_today:
                        gas["daily_m3"] += consumption_m3
                        gas["daily_kwh"] += consumption_kwh

        # Fetch current electricity spot price
        current_price_mwh = await self.price_api.get_current_price()
        current_price_kwh = current_price_mwh / 1000 if current_price_mwh is not None else None

        # Fetch this month's prices for average cost calculation
        monthly_prices = await self.price_api.get_prices(start=month_start, end=now)
        avg_price_mwh = None
        if monthly_prices:
            avg_price_mwh = sum(p["price"] for p in monthly_prices) / len(monthly_prices)
        avg_price_kwh = avg_price_mwh / 1000 if avg_price_mwh is not None else None

        # Calculate apartment share
        apartment_area = self.config_entry.options.get(
            CONF_APARTMENT_AREA,
            self.config_entry.data.get(CONF_APARTMENT_AREA, 0),
        )
        building_area = self.config_entry.options.get(
            CONF_BUILDING_AREA,
            self.config_entry.data.get(CONF_BUILDING_AREA, 0),
        )

        area_ratio = apartment_area / building_area if building_area > 0 else 0

        result = {
            "has_electricity": len(electricity_eics) > 0,
            "has_gas": len(gas_eics) > 0,
            "area_ratio": area_ratio,
            "electricity": {
                "building_daily_kwh": round(electricity["daily_kwh"], 2),
                "building_monthly_kwh": round(electricity["monthly_kwh"], 2),
            },
            "gas": {
                "building_daily_m3": round(gas["daily_m3"], 2),
                "building_monthly_m3": round(gas["monthly_m3"], 2),
                "building_daily_kwh": round(gas["daily_kwh"], 2),
                "building_monthly_kwh": round(gas["monthly_kwh"], 2),
                "apartment_daily_m3": round(gas["daily_m3"] * area_ratio, 2),
                "apartment_monthly_m3": round(gas["monthly_m3"] * area_ratio, 2),
                "apartment_daily_kwh": round(gas["daily_kwh"] * area_ratio, 2),
                "apartment_monthly_kwh": round(gas["monthly_kwh"] * area_ratio, 2),
            },
            "price": {
                "electricity_spot_eur_kwh": round(current_price_kwh, 4) if current_price_kwh is not None else None,
                "electricity_avg_eur_kwh": round(avg_price_kwh, 4) if avg_price_kwh is not None else None,
            },
        }

        _LOGGER.debug("Estfeed data updated: %s", result)
        return result
