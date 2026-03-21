"""DataUpdateCoordinator for Estfeed gas integration with predictive estimation."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EstfeedApiClient,
    EstfeedApiError,
    EstfeedAuthError,
    OpenMeteoClient,
)
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN, get_area_config

_LOGGER = logging.getLogger(__name__)


def _linear_regression(
    x: list[float], y: list[float]
) -> tuple[float, float] | None:
    """Simple OLS linear regression. Returns (slope, intercept) or None."""
    n = len(x)
    if n < 3 or len(y) != n:
        return None
    sum_x = sum(x)
    sum_y = sum(y)
    sum_xx = sum(xi * xi for xi in x)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-10:
        return None
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


class EstfeedDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch Estfeed gas data with weather-based estimation."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        estfeed_api: EstfeedApiClient,
        weather_api: OpenMeteoClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
        )
        self.estfeed_api = estfeed_api
        self.weather_api = weather_api
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
        """Fetch gas data and compute estimation for the gap period."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        gas_eics = [
            mp["eic"]
            for mp in self.metering_points
            if mp["commodityType"] == "NATURAL_GAS"
        ]

        if not gas_eics:
            return self._empty_result()

        # Always fetch full 31-day window (API max) so regression has
        # enough data even on brand new install or early in a month
        fetch_start = now - timedelta(days=31)

        try:
            metering_data = await self.estfeed_api.get_metering_data(
                start=fetch_start,
                end=now,
                resolution="one_hour",
                eics=gas_eics,
            )
        except EstfeedAuthError as err:
            raise ConfigEntryAuthFailed from err
        except EstfeedApiError as err:
            raise UpdateFailed(f"Failed to fetch metering data: {err}") from err

        # Parse hourly gas intervals
        hourly_data = self._parse_hourly_gas(metering_data, gas_eics)

        # Compute actuals
        month_actual_m3 = sum(
            h["m3"] for h in hourly_data if h["dt"] >= month_start
        )
        month_actual_kwh = sum(
            h["kwh"] for h in hourly_data if h["dt"] >= month_start
        )

        # Find the last hour with actual data and today's actual
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_actual_m3 = sum(
            h["m3"] for h in hourly_data if h["dt"] >= today_start
        )

        last_actual_dt = None
        if hourly_data:
            last_actual_dt = max(h["dt"] for h in hourly_data if h["m3"] > 0 or h["kwh"] > 0)

        # Compute latest calorific value (kWh per m³) from recent data
        calorific = self._compute_calorific_value(hourly_data)

        # Fetch weather data for estimation
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        temperatures = await self.weather_api.get_hourly_temperatures(
            lat, lon, past_days=31, forecast_days=1
        )

        # Estimate gap consumption and predicted daily rate
        estimated_m3 = 0.0
        predicted_daily_m3 = 0.0
        is_estimated = False
        if last_actual_dt and last_actual_dt < now - timedelta(hours=1):
            estimated_m3, predicted_daily_m3 = self._estimate_gap(
                hourly_data, temperatures, last_actual_dt, now
            )
            is_estimated = estimated_m3 > 0

        total_m3 = month_actual_m3 + estimated_m3
        total_kwh = month_actual_kwh + estimated_m3 * calorific
        today_m3 = today_actual_m3
        if last_actual_dt and last_actual_dt < now and last_actual_dt >= today_start:
            today_m3 += estimated_m3  # Add estimate for today's gap

        # Apply apartment area ratio
        apartment_area, building_area = get_area_config(self.config_entry)
        area_ratio = apartment_area / building_area if building_area > 0 else 0

        # Flow rate: predicted daily m³ / 24h, scaled to apartment
        flow_rate_m3h = (predicted_daily_m3 / 24) * area_ratio if predicted_daily_m3 > 0 else 0

        return {
            "has_gas": True,
            "area_ratio": area_ratio,
            "is_estimated": is_estimated,
            "gas": {
                "apartment_total_m3": round(total_m3 * area_ratio, 2),
                "apartment_total_kwh": round(total_kwh * area_ratio, 2),
                "apartment_today_m3": round(today_m3 * area_ratio, 2),
                "apartment_flow_rate_m3h": round(flow_rate_m3h, 3),
            },
        }

    def _empty_result(self) -> dict[str, Any]:
        """Return empty result when no gas data is available."""
        return {
            "has_gas": False,
            "area_ratio": 0,
            "is_estimated": False,
            "gas": {
                "apartment_total_m3": 0.0,
                "apartment_total_kwh": 0.0,
                "apartment_today_m3": 0.0,
                "apartment_flow_rate_m3h": 0.0,
            },
        }

    def _parse_hourly_gas(
        self,
        metering_data: list[dict[str, Any]],
        gas_eics: list[str],
    ) -> list[dict[str, Any]]:
        """Parse hourly gas intervals from API response.

        Returns list of {dt: datetime, m3: float, kwh: float} sorted by time.
        """
        result: list[dict[str, Any]] = []
        for mp_data in metering_data:
            eic = mp_data.get("meteringPointEic", "")
            if eic not in gas_eics or mp_data.get("error"):
                continue
            for interval in mp_data.get("accountingIntervals", []):
                m3 = interval.get("consumptionM3")
                kwh = interval.get("consumptionKwh")
                if m3 is None and kwh is None:
                    continue  # Empty future slot
                period_start = interval.get("periodStart", "")
                try:
                    dt = datetime.strptime(
                        period_start, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                result.append({
                    "dt": dt,
                    "m3": m3 or 0.0,
                    "kwh": kwh or 0.0,
                })
        result.sort(key=lambda h: h["dt"])
        return result

    def _compute_calorific_value(
        self, hourly_data: list[dict[str, Any]]
    ) -> float:
        """Compute average kWh/m³ calorific value from recent data."""
        total_kwh = 0.0
        total_m3 = 0.0
        # Use last 48 hours of data for a recent average
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for h in hourly_data:
            if h["dt"] >= cutoff and h["m3"] > 0:
                total_kwh += h["kwh"]
                total_m3 += h["m3"]
        if total_m3 > 0:
            return total_kwh / total_m3
        # Fallback: Estonian natural gas typical calorific value
        return 10.6

    @staticmethod
    def _daily_avg_temp(
        day_key: str, temperatures: dict[datetime, float]
    ) -> float | None:
        """Compute average temperature for a given day."""
        day_temps = []
        for hour in range(24):
            dt = datetime.strptime(day_key, "%Y-%m-%d").replace(
                hour=hour, tzinfo=timezone.utc
            )
            if dt in temperatures:
                day_temps.append(temperatures[dt])
        return sum(day_temps) / len(day_temps) if day_temps else None

    def _estimate_gap(
        self,
        hourly_data: list[dict[str, Any]],
        temperatures: dict[datetime, float],
        last_actual_dt: datetime,
        now: datetime,
    ) -> tuple[float, float]:
        """Estimate gas consumption for the gap between last actual data and now.

        Uses thermal-inertia-aware regression: weighted temperature
        (45% today + 55% yesterday) accounts for building thermal mass.
        Both gas history (Estfeed, 31 days) and temperature history
        (Open-Meteo, 31+ days) are always available.

        Returns (estimated_gap_m3, predicted_daily_m3).
        """
        # Build daily aggregates
        daily: dict[str, dict[str, float]] = {}
        for h in hourly_data:
            day_key = h["dt"].strftime("%Y-%m-%d")
            if day_key not in daily:
                daily[day_key] = {"m3": 0.0, "hours": 0}
            daily[day_key]["m3"] += h["m3"]
            daily[day_key]["hours"] += 1

        # Build sorted list of complete days with avg temperatures
        daily_temps: dict[str, float] = {}
        for day_key, agg in daily.items():
            if agg["hours"] < 20:
                continue
            avg_t = self._daily_avg_temp(day_key, temperatures)
            if avg_t is not None:
                daily_temps[day_key] = avg_t

        # Build regression pairs using weighted temp (45% today + 55% yesterday)
        sorted_days = sorted(daily_temps.keys())
        weighted_temps: list[float] = []
        daily_m3: list[float] = []
        for i in range(1, len(sorted_days)):
            today_key = sorted_days[i]
            yesterday_key = sorted_days[i - 1]
            weighted = 0.45 * daily_temps[today_key] + 0.55 * daily_temps[yesterday_key]
            weighted_temps.append(weighted)
            daily_m3.append(daily[today_key]["m3"])

        gap_hours = max(1, int((now - last_actual_dt).total_seconds() / 3600))

        regression = _linear_regression(weighted_temps, daily_m3)
        if regression is None:
            return 0.0, 0.0

        slope, intercept = regression

        # Compute weighted temperature for the gap period
        # "today" = avg temp of gap hours, "yesterday" = avg temp of previous day
        gap_temps = []
        for i in range(gap_hours):
            gap_dt = (last_actual_dt + timedelta(hours=i + 1)).replace(
                minute=0, second=0, microsecond=0
            )
            if gap_dt in temperatures:
                gap_temps.append(temperatures[gap_dt])

        if not gap_temps:
            return 0.0, 0.0

        yesterday_key = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_avg = self._daily_avg_temp(yesterday_key, temperatures)
        if yesterday_avg is None:
            yesterday_avg = sum(gap_temps) / len(gap_temps)

        today_avg = sum(gap_temps) / len(gap_temps)
        weighted_temp = 0.45 * today_avg + 0.55 * yesterday_avg

        predicted_daily_m3 = max(0, slope * weighted_temp + intercept)
        estimated = predicted_daily_m3 * (gap_hours / 24)

        _LOGGER.debug(
            "Gap estimation: %d hours, today_temp=%.1f°C, "
            "yesterday_temp=%.1f°C, weighted=%.1f°C, "
            "predicted_daily=%.1f m³, estimated_gap=%.1f m³",
            gap_hours, today_avg, yesterday_avg, weighted_temp,
            predicted_daily_m3, estimated,
        )
        return round(estimated, 2), round(predicted_daily_m3, 2)
