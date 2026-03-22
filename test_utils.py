"""Shared utilities for PV24 test/backtest scripts.

Centralises algorithms and API fetch helpers that were previously
duplicated across test_api.py, test_sensors.py, and backtest_profile.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import sys
from pathlib import Path

import aiohttp

# Add pv24 component to path so we can import const without triggering
# homeassistant imports from __init__.py
sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "pv24"))
from const import (  # noqa: E402
    BASE_URL,
    DEFAULT_LAT,
    DEFAULT_LON,
    MIN_COMPLETE_DAY_HOURS,
    OPEN_METEO_URL,
    THERMAL_WEIGHTS,
    TOKEN_URL,
)
sys.path.pop(0)

# ---------------------------------------------------------------------------
# Core algorithms (mirrors coordinator.py logic)
# ---------------------------------------------------------------------------


def linear_regression(
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


def build_hourly_profile(hourly: list[dict[str, Any]]) -> list[float]:
    """Build normalized 24-hour consumption profile from parsed hourly data."""
    days: dict[str, list[dict[str, Any]]] = {}
    for h in hourly:
        day_key = h["dt"].strftime("%Y-%m-%d")
        days.setdefault(day_key, []).append(h)

    hour_fraction_sums = [0.0] * 24
    count = 0
    for hours in days.values():
        if len(hours) < MIN_COMPLETE_DAY_HOURS:
            continue
        day_total = sum(h["m3"] for h in hours)
        if day_total <= 0:
            continue
        count += 1
        for h in hours:
            hour_fraction_sums[h["dt"].hour] += h["m3"] / day_total

    if count == 0:
        return [1.0 / 24] * 24
    profile = [s / count for s in hour_fraction_sums]
    profile_sum = sum(profile)
    if profile_sum <= 0:
        return [1.0 / 24] * 24
    return [p / profile_sum for p in profile]


def build_hourly_profile_from_intervals(gas_intervals: list[dict]) -> list[float]:
    """Build normalized 24-hour profile from raw API intervals (periodStart keyed)."""
    days: dict[str, list[dict]] = {}
    for iv in gas_intervals:
        day = iv["periodStart"][:10]
        days.setdefault(day, []).append(iv)

    hour_fraction_sums = [0.0] * 24
    count = 0
    for hours in days.values():
        if len(hours) < MIN_COMPLETE_DAY_HOURS:
            continue
        day_total = sum(h.get("consumptionM3", 0) for h in hours)
        if day_total <= 0:
            continue
        count += 1
        for h in hours:
            hour = int(h["periodStart"][11:13])
            hour_fraction_sums[hour] += h.get("consumptionM3", 0) / day_total

    if count == 0:
        return [1.0 / 24] * 24
    profile = [s / count for s in hour_fraction_sums]
    profile_sum = sum(profile)
    if profile_sum <= 0:
        return [1.0 / 24] * 24
    return [p / profile_sum for p in profile]


def daily_avg_temp(
    day_key: str, temperatures: dict[datetime, float]
) -> float | None:
    """Compute average temperature for a single day."""
    temps = []
    for hour in range(24):
        dt = datetime.strptime(day_key, "%Y-%m-%d").replace(
            hour=hour, tzinfo=timezone.utc
        )
        if dt in temperatures:
            temps.append(temperatures[dt])
    return sum(temps) / len(temps) if temps else None


def estimate_gap(
    hourly: list[dict[str, Any]],
    temps: dict[datetime, float],
    last_actual: datetime,
    now: datetime,
) -> tuple[float, float, list[float]]:
    """Estimate gas consumption for the gap between last actual data and now."""
    daily: dict[str, dict[str, float]] = {}
    for h in hourly:
        dk = h["dt"].strftime("%Y-%m-%d")
        if dk not in daily:
            daily[dk] = {"m3": 0.0, "hours": 0}
        daily[dk]["m3"] += h["m3"]
        daily[dk]["hours"] += 1

    daily_temps: dict[str, float] = {}
    for dk, agg in daily.items():
        if agg["hours"] < MIN_COMPLETE_DAY_HOURS:
            continue
        t = daily_avg_temp(dk, temps)
        if t is not None:
            daily_temps[dk] = t

    sorted_days = sorted(daily_temps.keys())
    w0, w1, w2 = THERMAL_WEIGHTS
    wt, dm = [], []
    for i in range(2, len(sorted_days)):
        wt.append(
            w0 * daily_temps[sorted_days[i]]
            + w1 * daily_temps[sorted_days[i - 1]]
            + w2 * daily_temps[sorted_days[i - 2]]
        )
        dm.append(daily[sorted_days[i]]["m3"])

    profile = build_hourly_profile(hourly)
    reg = linear_regression(wt, dm)
    if reg is None:
        return 0.0, 0.0, profile

    slope, intercept = reg
    gap_hours = max(1, int((now - last_actual).total_seconds() / 3600))
    gap_temps = [
        temps[dt]
        for i in range(gap_hours)
        if (
            dt := (last_actual + timedelta(hours=i + 1)).replace(
                minute=0, second=0, microsecond=0
            )
        )
        in temps
    ]
    if not gap_temps:
        return 0.0, 0.0, profile

    today_avg = sum(gap_temps) / len(gap_temps)
    yest = (
        daily_avg_temp((now - timedelta(days=1)).strftime("%Y-%m-%d"), temps)
        or today_avg
    )
    d2 = (
        daily_avg_temp((now - timedelta(days=2)).strftime("%Y-%m-%d"), temps)
        or yest
    )
    weighted = w0 * today_avg + w1 * yest + w2 * d2
    pred_daily = max(0, slope * weighted + intercept)

    gap_weight = sum(
        profile[
            (last_actual + timedelta(hours=i + 1))
            .replace(minute=0, second=0, microsecond=0)
            .hour
        ]
        for i in range(gap_hours)
    )
    return round(pred_daily * gap_weight, 2), round(pred_daily, 2), profile


# ---------------------------------------------------------------------------
# Gas data parsing
# ---------------------------------------------------------------------------


def parse_hourly_gas(
    metering_data: list[dict[str, Any]], gas_eics: list[str]
) -> list[dict[str, Any]]:
    """Parse hourly gas intervals from API response into [{dt, m3, kwh}]."""
    result = []
    for mp_data in metering_data:
        eic = mp_data.get("meteringPointEic", "")
        if eic not in gas_eics or mp_data.get("error"):
            continue
        for iv in mp_data.get("accountingIntervals", []):
            m3 = iv.get("consumptionM3")
            kwh = iv.get("consumptionKwh")
            if m3 is None and kwh is None:
                continue
            try:
                dt = datetime.strptime(
                    iv["periodStart"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                continue
            result.append({"dt": dt, "m3": m3 or 0.0, "kwh": kwh or 0.0})
    result.sort(key=lambda h: h["dt"])
    return result


def compute_calorific(
    hourly: list[dict[str, Any]], now: datetime, default: float = 10.6
) -> float:
    """Compute average kWh/m³ calorific value from last 48 hours."""
    cutoff = now - timedelta(hours=48)
    total_kwh = sum(h["kwh"] for h in hourly if h["dt"] >= cutoff and h["m3"] > 0)
    total_m3 = sum(h["m3"] for h in hourly if h["dt"] >= cutoff and h["m3"] > 0)
    return total_kwh / total_m3 if total_m3 > 0 else default


# ---------------------------------------------------------------------------
# API fetch helpers
# ---------------------------------------------------------------------------


async def fetch_token(
    session: aiohttp.ClientSession, client_id: str, client_secret: str
) -> str:
    """Authenticate and return access token."""
    async with session.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Auth failed: {resp.status} {await resp.text()}")
        return (await resp.json())["access_token"]


async def fetch_metering_points(
    session: aiohttp.ClientSession, token: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Fetch metering point EICs."""
    params = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    async with session.get(
        f"{BASE_URL}/api/public/v1/metering-point-eics",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Metering points: {resp.status}")
        return await resp.json()


async def fetch_gas_data(
    session: aiohttp.ClientSession,
    token: str,
    start: datetime,
    end: datetime,
    eics: str | list[str],
) -> list[dict[str, Any]]:
    """Fetch raw metering data from the Estfeed API."""
    await asyncio.sleep(6)  # rate limit
    eic_str = eics if isinstance(eics, str) else ",".join(eics)
    params = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_hour",
        "meteringPointEics": eic_str,
    }
    async with session.get(
        f"{BASE_URL}/api/public/v1/metering-data",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Metering data: {resp.status}")
        return await resp.json()


async def fetch_gas_data_parsed(
    session: aiohttp.ClientSession,
    token: str,
    start: datetime,
    end: datetime,
    eic: str,
) -> list[dict[str, Any]]:
    """Fetch and parse gas data into [{dt, m3, kwh}] dicts."""
    raw = await fetch_gas_data(session, token, start, end, eic)
    result = []
    for mp in raw:
        for iv in mp.get("accountingIntervals", []):
            m3 = iv.get("consumptionM3")
            if m3 is None:
                continue
            dt = datetime.strptime(
                iv["periodStart"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            result.append({"dt": dt, "m3": m3, "kwh": iv.get("consumptionKwh", 0)})
    return result


async def fetch_weather(
    session: aiohttp.ClientSession,
    past_days: int = 31,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
) -> dict[datetime, float]:
    """Fetch hourly temperatures from Open-Meteo."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "past_days": past_days,
        "forecast_days": 1,
        "timeformat": "iso8601",
        "timezone": "UTC",
    }
    async with session.get(OPEN_METEO_URL, params=params) as resp:
        if resp.status != 200:
            return {}
        data = await resp.json()
    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])
    return {
        datetime.fromisoformat(t).replace(tzinfo=timezone.utc): temp
        for t, temp in zip(times, temps)
        if temp is not None
    }
