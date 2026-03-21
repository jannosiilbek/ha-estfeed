"""
Estfeed Gas & Open-Meteo API validation script.
Tests gas hourly data, weather temperatures, and estimation logic.

Usage: python3 test_api.py <client_id> <client_secret>
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass

import aiohttp

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Tallinn default coordinates (overridden by HA home location in production)
DEFAULT_LAT = 59.437
DEFAULT_LON = 24.7536


def linear_regression(x: list[float], y: list[float]) -> tuple[float, float] | None:
    """Simple OLS. Returns (slope, intercept) or None."""
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


def _build_hourly_profile(gas_intervals: list[dict]) -> list[float]:
    """Build normalized 24-hour consumption profile from raw API intervals."""
    days: dict[str, list[dict]] = {}
    for iv in gas_intervals:
        day = iv["periodStart"][:10]
        days.setdefault(day, []).append(iv)

    hour_fraction_sums = [0.0] * 24
    count = 0
    for hours in days.values():
        if len(hours) < 20:
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


async def test_auth(session: aiohttp.ClientSession, client_id: str, client_secret: str) -> str | None:
    """Test 1: Authenticate."""
    print("\n=== Test 1: Authentication ===")
    try:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                print(f"  FAIL: HTTP {resp.status} - {await resp.text()}")
                return None
            data = await resp.json()
            print(f"  OK: Token received (expires in {data.get('expires_in')}s)")
            return data["access_token"]
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


async def test_metering_points(session: aiohttp.ClientSession, token: str) -> list[str]:
    """Test 2: Find gas metering points."""
    print("\n=== Test 2: Metering Points ===")
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        async with session.get(
            f"{BASE_URL}/api/public/v1/metering-point-eics",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                print(f"  FAIL: HTTP {resp.status}")
                return []
            data = await resp.json()
            gas_eics = []
            for mp in data:
                eic = mp.get("eic", "?")
                commodity = mp.get("commodityType", "?")
                print(f"  - EIC: {eic} | Type: {commodity}")
                if commodity == "NATURAL_GAS":
                    gas_eics.append(eic)
            print(f"  Gas EICs: {gas_eics}")
            return gas_eics
    except Exception as e:
        print(f"  FAIL: {e}")
        return []


async def test_hourly_gas(session: aiohttp.ClientSession, token: str, gas_eics: list[str]) -> list[dict]:
    """Test 3: Fetch hourly gas data for last 7 days."""
    print("\n=== Test 3: Hourly Gas Data (last 7 days) ===")
    now = datetime.now(timezone.utc)
    await asyncio.sleep(6)  # Rate limit
    params = {
        "startDateTime": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_hour",
        "meteringPointEics": ",".join(gas_eics),
    }
    try:
        async with session.get(
            f"{BASE_URL}/api/public/v1/metering-data",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                print(f"  FAIL: HTTP {resp.status}")
                return []
            data = await resp.json()

            all_intervals = []
            for mp in data:
                eic = mp.get("meteringPointEic", "?")
                intervals = mp.get("accountingIntervals", [])
                with_data = [i for i in intervals if i.get("consumptionM3") is not None]
                empty = [i for i in intervals if i.get("consumptionM3") is None]
                print(f"  EIC: {eic}")
                print(f"    Total intervals: {len(intervals)}, with data: {len(with_data)}, empty: {len(empty)}")

                if with_data:
                    last = with_data[-1]
                    last_ts = datetime.strptime(last["periodStart"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    lag = (now - last_ts).total_seconds() / 3600
                    print(f"    Last data: {last['periodStart']} ({last.get('consumptionM3')} m³)")
                    print(f"    Data lag: ~{lag:.1f} hours")
                    all_intervals.extend(with_data)

                # Daily aggregates
                daily: dict[str, dict[str, float]] = {}
                for i in with_data:
                    day = i["periodStart"][:10]
                    if day not in daily:
                        daily[day] = {"m3": 0.0, "kwh": 0.0, "hours": 0}
                    daily[day]["m3"] += i.get("consumptionM3", 0)
                    daily[day]["kwh"] += i.get("consumptionKwh", 0)
                    daily[day]["hours"] += 1

                print(f"    Daily breakdown:")
                for day, agg in sorted(daily.items()):
                    ratio = agg["kwh"] / agg["m3"] if agg["m3"] > 0 else 0
                    print(f"      {day}: {agg['m3']:.0f} m³ / {agg['kwh']:.1f} kWh"
                          f" ({ratio:.2f} kWh/m³, {agg['hours']}h)")

            return all_intervals
    except Exception as e:
        print(f"  FAIL: {e}")
        return []


async def test_weather(session: aiohttp.ClientSession) -> dict[datetime, float]:
    """Test 4: Fetch weather data from Open-Meteo."""
    print("\n=== Test 4: Open-Meteo Weather (last 7 days + forecast) ===")
    params = {
        "latitude": DEFAULT_LAT,
        "longitude": DEFAULT_LON,
        "hourly": "temperature_2m",
        "past_days": 7,
        "forecast_days": 1,
        "timeformat": "iso8601",
        "timezone": "UTC",
    }
    try:
        async with session.get(OPEN_METEO_URL, params=params) as resp:
            if resp.status != 200:
                print(f"  FAIL: HTTP {resp.status}")
                return {}
            data = await resp.json()
            times = data["hourly"]["time"]
            temps = data["hourly"]["temperature_2m"]
            result = {}
            for t, temp in zip(times, temps):
                if temp is not None:
                    dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                    result[dt] = temp
            print(f"  OK: {len(result)} hourly temperatures")

            # Daily averages
            daily_temps: dict[str, list[float]] = {}
            for dt, temp in result.items():
                day = dt.strftime("%Y-%m-%d")
                daily_temps.setdefault(day, []).append(temp)
            for day, temps_list in sorted(daily_temps.items()):
                avg = sum(temps_list) / len(temps_list)
                print(f"    {day}: avg {avg:.1f}°C (min {min(temps_list):.1f}, max {max(temps_list):.1f})")
            return result
    except Exception as e:
        print(f"  FAIL: {e}")
        return {}


def test_estimation(gas_intervals: list[dict], temperatures: dict[datetime, float]) -> None:
    """Test 5: Run estimation algorithm on real data."""
    print("\n=== Test 5: Estimation Algorithm ===")

    # Build daily aggregates from gas data
    daily: dict[str, dict[str, float]] = {}
    for i in gas_intervals:
        day = i["periodStart"][:10]
        if day not in daily:
            daily[day] = {"m3": 0.0, "hours": 0}
        daily[day]["m3"] += i.get("consumptionM3", 0)
        daily[day]["hours"] += 1

    # Match with daily average temperatures
    pairs: list[dict] = []
    for day, agg in sorted(daily.items()):
        if agg["hours"] < 20:  # Skip incomplete days
            continue
        day_temps = []
        for hour in range(24):
            dt = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
            if dt in temperatures:
                day_temps.append(temperatures[dt])
        if not day_temps:
            continue
        avg_temp = sum(day_temps) / len(day_temps)
        pairs.append({"day": day, "avg_temp": avg_temp, "m3": agg["m3"]})
        print(f"  {day}: {agg['m3']:.0f} m³ at {avg_temp:.1f}°C avg")

    if len(pairs) < 3:
        print("  Not enough complete days for regression")
        return

    # Linear regression
    x = [p["avg_temp"] for p in pairs]
    y = [p["m3"] for p in pairs]
    result = linear_regression(x, y)
    if result is None:
        print("  Regression failed")
        return

    slope, intercept = result
    print(f"\n  Regression: daily_m3 = {slope:.2f} × avg_temp + {intercept:.1f}")
    print(f"  Interpretation: each 1°C warmer → {abs(slope):.1f} m³ {'less' if slope < 0 else 'more'} gas/day")

    # Predict for a range of temperatures
    print(f"\n  Predictions:")
    for temp in [-10, -5, 0, 5, 10, 15, 20]:
        predicted = slope * temp + intercept
        predicted = max(0, predicted)
        print(f"    {temp:+3d}°C → {predicted:.0f} m³/day")

    # Estimate current gap
    now = datetime.now(timezone.utc)
    # Find last data hour
    last_data_times = [
        datetime.strptime(i["periodStart"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        for i in gas_intervals
    ]
    last_data_dt = max(last_data_times) if last_data_times else None
    if last_data_dt:
        gap_hours = int((now - last_data_dt).total_seconds() / 3600)
        gap_temps = []
        for h in range(gap_hours):
            dt = (last_data_dt + timedelta(hours=h + 1)).replace(minute=0, second=0, microsecond=0)
            if dt in temperatures:
                gap_temps.append(temperatures[dt])
        if gap_temps:
            avg_gap_temp = sum(gap_temps) / len(gap_temps)
            predicted_daily = max(0, slope * avg_gap_temp + intercept)

            # Build hourly consumption profile
            hourly_profile = _build_hourly_profile(gas_intervals)
            gap_weight = 0.0
            for h in range(gap_hours):
                dt = (last_data_dt + timedelta(hours=h + 1)).replace(minute=0, second=0, microsecond=0)
                gap_weight += hourly_profile[dt.hour]
            estimated_gap = predicted_daily * gap_weight

            print(f"\n  Current gap: {gap_hours}h, avg temp {avg_gap_temp:.1f}°C")
            print(f"  Estimated gap consumption: {estimated_gap:.1f} m³ (building, hourly profile)")
        else:
            print(f"\n  No forecast temperatures available for gap period")


async def main():
    print("Estfeed Gas & Weather API Validation")
    print("=" * 45)

    if len(sys.argv) >= 3:
        client_id = sys.argv[1]
        client_secret = sys.argv[2]
    else:
        client_id = input("Enter client_id: ").strip()
        client_secret = getpass("Enter client_secret: ").strip()

    if not client_id or not client_secret:
        print("Error: client_id and client_secret are required")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        # Test 1: Auth
        token = await test_auth(session, client_id, client_secret)
        if not token:
            print("\nAuthentication failed.")
            sys.exit(1)

        # Test 2: Find gas EICs
        gas_eics = await test_metering_points(session, token)
        if not gas_eics:
            print("\nNo gas metering points found.")
            sys.exit(1)

        # Test 3: Hourly gas data
        gas_intervals = await test_hourly_gas(session, token, gas_eics)

        # Test 4: Weather data
        temperatures = await test_weather(session)

        # Test 5: Estimation
        if gas_intervals and temperatures:
            test_estimation(gas_intervals, temperatures)

    print("\n" + "=" * 45)
    print("Validation complete!")


if __name__ == "__main__":
    asyncio.run(main())
