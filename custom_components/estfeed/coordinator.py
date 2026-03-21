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
    ElectricityPriceClient,
    EstfeedApiClient,
    EstfeedApiError,
    EstfeedAuthError,
    GasPriceClient,
    OpenMeteoClient,
)
from .const import ELECTRICITY_PRICE_UPDATE_INTERVAL
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN, get_area_config

_LOGGER = logging.getLogger(__name__)

# Thermal inertia weights — validated over 196 days (Sep 2025–Mar 2026).
# Building thermal mass means gas usage lags temperature by 1-2 days.
# 3-day model (MAPE 8.1%, MAE 3.0 m³) outperforms 2-day (8.6%, 3.2 m³).
THERMAL_WEIGHTS = (0.40, 0.40, 0.20)  # today, yesterday, day-before

MIN_COMPLETE_DAY_HOURS = 20  # Skip days with fewer hours of data
DEFAULT_CALORIFIC_KWH_M3 = 10.6  # Estonian natural gas typical value


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


def _make_result(
    has_gas: bool = False,
    area_ratio: float = 0,
    is_estimated: bool = False,
    total_m3: float = 0.0,
    total_kwh: float = 0.0,
    today_m3: float = 0.0,
    flow_rate_m3h: float = 0.0,
) -> dict[str, Any]:
    """Build the coordinator result dict. Single source of truth for the shape."""
    return {
        "has_gas": has_gas,
        "area_ratio": area_ratio,
        "is_estimated": is_estimated,
        "gas": {
            "apartment_total_m3": round(total_m3, 2),
            "apartment_total_kwh": round(total_kwh, 2),
            "apartment_today_m3": round(today_m3, 2),
            "apartment_flow_rate_m3h": round(flow_rate_m3h, 3),
        },
    }


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
            return _make_result()

        # Always fetch full 31-day window (API max) so regression has
        # enough data even on brand new install or early in a month
        try:
            metering_data = await self.estfeed_api.get_metering_data(
                start=now - timedelta(days=31),
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

        last_actual_dt = max(
            (h["dt"] for h in hourly_data if h["m3"] > 0 or h["kwh"] > 0),
            default=None,
        )

        # Compute latest calorific value (kWh per m³) from recent data
        calorific = self._compute_calorific_value(hourly_data, now)

        # Fetch weather data for estimation
        temperatures = await self.weather_api.get_hourly_temperatures(
            self.hass.config.latitude,
            self.hass.config.longitude,
            past_days=31,
            forecast_days=1,
        )

        # Estimate gap consumption and predicted daily rate
        estimated_m3 = 0.0
        predicted_daily_m3 = 0.0
        hourly_profile: list[float] | None = None
        is_estimated = False
        if last_actual_dt and last_actual_dt < now - timedelta(hours=1):
            estimated_m3, predicted_daily_m3, hourly_profile = self._estimate_gap(
                hourly_data, temperatures, last_actual_dt, now
            )
            is_estimated = estimated_m3 > 0

        total_m3 = month_actual_m3 + estimated_m3
        total_kwh = month_actual_kwh + estimated_m3 * calorific
        today_m3 = today_actual_m3
        if last_actual_dt and last_actual_dt < now and last_actual_dt >= today_start:
            today_m3 += estimated_m3

        # Apply apartment area ratio
        apartment_area, building_area = get_area_config(self.config_entry)
        area_ratio = apartment_area / building_area if building_area > 0 else 0

        # Flow rate: use hourly profile for current hour, scaled to apartment
        if predicted_daily_m3 > 0 and hourly_profile is not None:
            flow_rate = predicted_daily_m3 * hourly_profile[now.hour] * area_ratio
        else:
            flow_rate = 0

        return _make_result(
            has_gas=True,
            area_ratio=area_ratio,
            is_estimated=is_estimated,
            total_m3=total_m3 * area_ratio,
            total_kwh=total_kwh * area_ratio,
            today_m3=today_m3 * area_ratio,
            flow_rate_m3h=flow_rate,
        )

    @staticmethod
    def _parse_hourly_gas(
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

    @staticmethod
    def _compute_calorific_value(
        hourly_data: list[dict[str, Any]], now: datetime
    ) -> float:
        """Compute average kWh/m³ calorific value from last 48 hours."""
        cutoff = now - timedelta(hours=48)
        total_kwh = sum(h["kwh"] for h in hourly_data if h["dt"] >= cutoff and h["m3"] > 0)
        total_m3 = sum(h["m3"] for h in hourly_data if h["dt"] >= cutoff and h["m3"] > 0)
        return total_kwh / total_m3 if total_m3 > 0 else DEFAULT_CALORIFIC_KWH_M3

    @staticmethod
    def _build_hourly_profile(
        hourly_data: list[dict[str, Any]],
    ) -> list[float]:
        """Build a normalized 24-hour consumption profile from historical data.

        For each complete day (>= MIN_COMPLETE_DAY_HOURS hours with nonzero
        total), compute each hour's fraction of that day's total.  Average the
        fractions across all qualifying days and normalize to sum=1.0.
        """
        days: dict[str, list[dict[str, Any]]] = {}
        for h in hourly_data:
            day_key = h["dt"].strftime("%Y-%m-%d")
            days.setdefault(day_key, []).append(h)

        hour_fraction_sums = [0.0] * 24
        complete_day_count = 0

        for hours in days.values():
            if len(hours) < MIN_COMPLETE_DAY_HOURS:
                continue
            day_total = sum(h["m3"] for h in hours)
            if day_total <= 0:
                continue
            complete_day_count += 1
            for h in hours:
                hour_fraction_sums[h["dt"].hour] += h["m3"] / day_total

        if complete_day_count == 0:
            return [1.0 / 24] * 24

        profile = [s / complete_day_count for s in hour_fraction_sums]
        profile_sum = sum(profile)
        if profile_sum <= 0:
            return [1.0 / 24] * 24
        return [p / profile_sum for p in profile]

    @staticmethod
    def _build_daily_avg_temps(
        daily: dict[str, dict[str, float]],
        temperatures: dict[datetime, float],
    ) -> dict[str, float]:
        """Build day_key -> avg_temp mapping for all complete days."""
        result: dict[str, float] = {}
        for day_key, agg in daily.items():
            if agg["hours"] < MIN_COMPLETE_DAY_HOURS:
                continue
            day_temps = []
            for hour in range(24):
                dt = datetime.strptime(day_key, "%Y-%m-%d").replace(
                    hour=hour, tzinfo=timezone.utc
                )
                if dt in temperatures:
                    day_temps.append(temperatures[dt])
            if day_temps:
                result[day_key] = sum(day_temps) / len(day_temps)
        return result

    @staticmethod
    def _daily_avg_temp(
        day_key: str, temperatures: dict[datetime, float]
    ) -> float | None:
        """Compute average temperature for a single day."""
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
    ) -> tuple[float, float, list[float]]:
        """Estimate gas consumption for the gap between last actual data and now.

        Uses thermal-inertia-aware regression: weighted temperature
        accounts for building thermal mass.  The gap is distributed using
        an hourly consumption profile rather than flat pro-rating.

        Returns (estimated_gap_m3, predicted_daily_m3, hourly_profile).
        """
        # Build daily aggregates
        daily: dict[str, dict[str, float]] = {}
        for h in hourly_data:
            day_key = h["dt"].strftime("%Y-%m-%d")
            if day_key not in daily:
                daily[day_key] = {"m3": 0.0, "hours": 0}
            daily[day_key]["m3"] += h["m3"]
            daily[day_key]["hours"] += 1

        # Build avg temps for all complete days at once
        daily_temps = self._build_daily_avg_temps(daily, temperatures)

        # Build regression pairs using 3-day weighted temp
        sorted_days = sorted(daily_temps.keys())
        w_today, w_yest, w_d2 = THERMAL_WEIGHTS
        weighted_temps: list[float] = []
        daily_m3: list[float] = []
        for i in range(2, len(sorted_days)):
            weighted = (
                w_today * daily_temps[sorted_days[i]]
                + w_yest * daily_temps[sorted_days[i - 1]]
                + w_d2 * daily_temps[sorted_days[i - 2]]
            )
            weighted_temps.append(weighted)
            daily_m3.append(daily[sorted_days[i]]["m3"])

        gap_hours = max(1, int((now - last_actual_dt).total_seconds() / 3600))

        profile = self._build_hourly_profile(hourly_data)

        regression = _linear_regression(weighted_temps, daily_m3)
        if regression is None:
            return 0.0, 0.0, profile

        slope, intercept = regression

        # Compute weighted temperature for the gap period
        gap_temps = []
        for i in range(gap_hours):
            gap_dt = (last_actual_dt + timedelta(hours=i + 1)).replace(
                minute=0, second=0, microsecond=0
            )
            if gap_dt in temperatures:
                gap_temps.append(temperatures[gap_dt])

        if not gap_temps:
            return 0.0, 0.0, profile

        today_avg = sum(gap_temps) / len(gap_temps)
        yesterday_key = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        day_before_key = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        yesterday_avg = self._daily_avg_temp(yesterday_key, temperatures) or today_avg
        day_before_avg = self._daily_avg_temp(day_before_key, temperatures) or yesterday_avg

        weighted_temp = (
            w_today * today_avg
            + w_yest * yesterday_avg
            + w_d2 * day_before_avg
        )

        predicted_daily_m3 = max(0, slope * weighted_temp + intercept)

        # Distribute using hourly consumption profile instead of flat /24
        gap_weight = 0.0
        for i in range(gap_hours):
            gap_dt = (last_actual_dt + timedelta(hours=i + 1)).replace(
                minute=0, second=0, microsecond=0
            )
            gap_weight += profile[gap_dt.hour]
        estimated = predicted_daily_m3 * gap_weight

        _LOGGER.debug(
            "Gap estimation: %d hours, temps=[%.1f, %.1f, %.1f]°C, "
            "weighted=%.1f°C, predicted_daily=%.1f m³, gap=%.1f m³",
            gap_hours, today_avg, yesterday_avg, day_before_avg,
            weighted_temp, predicted_daily_m3, estimated,
        )
        return round(estimated, 2), round(predicted_daily_m3, 2), profile


GAS_PRICE_UPDATE_INTERVAL = 3600  # 1 hour — price changes daily


class GasPriceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch daily gas exchange price from Elering."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        gas_price_api: GasPriceClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_gas_price",
            config_entry=config_entry,
            update_interval=timedelta(seconds=GAS_PRICE_UPDATE_INTERVAL),
        )
        self.gas_price_api = gas_price_api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch today's gas exchange price."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Fetch today + yesterday (in case today's price isn't published yet)
        entries = await self.gas_price_api.get_gas_price(
            start=today_start - timedelta(days=1),
            end=now,
        )

        if not entries:
            raise UpdateFailed("No gas price data available")

        # Use the most recent price entry
        latest = max(entries, key=lambda e: e["timestamp"])
        price_eur_mwh = latest["price"]
        price_timestamp = datetime.fromtimestamp(
            latest["timestamp"], tz=timezone.utc
        )

        return {
            "price_eur_mwh": round(price_eur_mwh, 3),
            "price_eur_kwh": round(price_eur_mwh / 1000, 6),
            "price_date": price_timestamp.strftime("%Y-%m-%d"),
        }


class ElectricityPriceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch NordPool Estonia electricity prices from Elering."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        electricity_price_api: ElectricityPriceClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_electricity_price",
            config_entry=config_entry,
            update_interval=timedelta(seconds=ELECTRICITY_PRICE_UPDATE_INTERVAL),
        )
        self.electricity_price_api = electricity_price_api

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch today's and tomorrow's electricity prices."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = today_start + timedelta(days=2)

        entries = await self.electricity_price_api.get_electricity_prices(
            start=today_start,
            end=tomorrow_end,
        )

        if not entries:
            raise UpdateFailed("No electricity price data available")

        # Partition into today and tomorrow
        tomorrow_start_ts = int((today_start + timedelta(days=1)).timestamp())
        today_entries = [e for e in entries if e["timestamp"] < tomorrow_start_ts]
        tomorrow_entries = [e for e in entries if e["timestamp"] >= tomorrow_start_ts]

        # Find current price: latest entry whose timestamp <= now
        now_ts = int(now.timestamp())
        current_entry = None
        for e in sorted(today_entries, key=lambda x: x["timestamp"]):
            if e["timestamp"] <= now_ts:
                current_entry = e
            else:
                break

        current_price = current_entry["price"] if current_entry else today_entries[0]["price"]

        # Today stats
        today_prices = [e["price"] for e in today_entries]
        today_avg = sum(today_prices) / len(today_prices)
        today_min = min(today_prices)
        today_max = max(today_prices)

        # Next hour price: find entry covering now + 1 hour
        next_hour_ts = now_ts + 3600
        next_hour_price = None
        for e in sorted(entries, key=lambda x: x["timestamp"]):
            if e["timestamp"] <= next_hour_ts:
                next_hour_price = e["price"]
            else:
                break

        # Build price lists for attributes
        def _format_entries(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "start": datetime.fromtimestamp(
                        e["timestamp"], tz=timezone.utc
                    ).isoformat(),
                    "price_eur_kwh": round(e["price"] / 1000, 6),
                }
                for e in sorted(raw, key=lambda x: x["timestamp"])
            ]

        return {
            "current_price_eur_kwh": round(current_price / 1000, 6),
            "today_avg_eur_kwh": round(today_avg / 1000, 6),
            "today_min_eur_kwh": round(today_min / 1000, 6),
            "today_max_eur_kwh": round(today_max / 1000, 6),
            "next_hour_eur_kwh": round(next_hour_price / 1000, 6) if next_hour_price is not None else None,
            "prices_today": _format_entries(today_entries),
            "prices_tomorrow": _format_entries(tomorrow_entries),
        }
