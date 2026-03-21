"""
Backtest: Hourly Profile vs Flat Gap Estimation for Gas Consumption.

Fetches ~90 days of real hourly gas data + weather, then simulates gap
estimation at various hours and gap lengths, comparing profile-weighted
vs flat (gap_hours/24) approaches against actual consumption.

Usage: python3 backtest_profile.py <client_id> <client_secret>
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Any

import aiohttp

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
EIC = "38ZEE-G0120307-8"
DEFAULT_LAT = 59.437
DEFAULT_LON = 24.7536
THERMAL_WEIGHTS = (0.40, 0.40, 0.20)
MIN_COMPLETE_DAY_HOURS = 20


# ---------------------------------------------------------------------------
# Core algorithms (same as coordinator.py / test_api.py)
# ---------------------------------------------------------------------------

def linear_regression(x: list[float], y: list[float]) -> tuple[float, float] | None:
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


def daily_avg_temp(day_key: str, temperatures: dict[datetime, float]) -> float | None:
    temps = []
    for hour in range(24):
        dt = datetime.strptime(day_key, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)
        if dt in temperatures:
            temps.append(temperatures[dt])
    return sum(temps) / len(temps) if temps else None


def predict_daily_m3(
    training_data: list[dict[str, Any]],
    temperatures: dict[datetime, float],
    target_day: str,
) -> float | None:
    """Run thermal-inertia-aware regression on training data, predict for target_day."""
    # Build daily aggregates
    daily: dict[str, dict[str, float]] = {}
    for h in training_data:
        day_key = h["dt"].strftime("%Y-%m-%d")
        if day_key not in daily:
            daily[day_key] = {"m3": 0.0, "hours": 0}
        daily[day_key]["m3"] += h["m3"]
        daily[day_key]["hours"] += 1

    # Build daily avg temps for complete days
    daily_temps: dict[str, float] = {}
    for day_key, agg in daily.items():
        if agg["hours"] < MIN_COMPLETE_DAY_HOURS:
            continue
        t = daily_avg_temp(day_key, temperatures)
        if t is not None:
            daily_temps[day_key] = t

    # Regression pairs using 3-day weighted temp
    sorted_days = sorted(daily_temps.keys())
    w0, w1, w2 = THERMAL_WEIGHTS
    weighted_temps: list[float] = []
    daily_m3: list[float] = []
    for i in range(2, len(sorted_days)):
        weighted = (
            w0 * daily_temps[sorted_days[i]]
            + w1 * daily_temps[sorted_days[i - 1]]
            + w2 * daily_temps[sorted_days[i - 2]]
        )
        weighted_temps.append(weighted)
        daily_m3.append(daily[sorted_days[i]]["m3"])

    reg = linear_regression(weighted_temps, daily_m3)
    if reg is None:
        return None

    slope, intercept = reg

    # Weighted temp for target day
    target_temp = daily_avg_temp(target_day, temperatures)
    d1 = (datetime.strptime(target_day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (datetime.strptime(target_day, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    t_yest = daily_avg_temp(d1, temperatures)
    t_d2 = daily_avg_temp(d2, temperatures)

    if target_temp is None:
        return None
    if t_yest is None:
        t_yest = target_temp
    if t_d2 is None:
        t_d2 = t_yest

    weighted = w0 * target_temp + w1 * t_yest + w2 * t_d2
    return max(0.0, slope * weighted + intercept)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def fetch_token(session: aiohttp.ClientSession, client_id: str, client_secret: str) -> str:
    async with session.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }) as resp:
        data = await resp.json()
        return data["access_token"]


async def fetch_gas_data(
    session: aiohttp.ClientSession, token: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Fetch hourly gas data for a date range, returns parsed [{dt, m3, kwh}]."""
    params = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_hour",
        "meteringPointEics": EIC,
    }
    async with session.get(
        f"{BASE_URL}/api/public/v1/metering-data",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    ) as resp:
        data = await resp.json()

    result = []
    for mp in data:
        for iv in mp.get("accountingIntervals", []):
            m3 = iv.get("consumptionM3")
            if m3 is None:
                continue
            dt = datetime.strptime(
                iv["periodStart"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            result.append({"dt": dt, "m3": m3, "kwh": iv.get("consumptionKwh", 0)})
    return result


async def fetch_weather(session: aiohttp.ClientSession, past_days: int) -> dict[datetime, float]:
    params = {
        "latitude": DEFAULT_LAT,
        "longitude": DEFAULT_LON,
        "hourly": "temperature_2m",
        "past_days": past_days,
        "forecast_days": 1,
        "timeformat": "iso8601",
        "timezone": "UTC",
    }
    async with session.get(OPEN_METEO_URL, params=params) as resp:
        data = await resp.json()
    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])
    result: dict[datetime, float] = {}
    for t, temp in zip(times, temps):
        if temp is not None:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            result[dt] = temp
    return result


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(
    all_hourly: list[dict[str, Any]],
    temperatures: dict[datetime, float],
) -> None:
    """Walk through historical data, simulate gaps, compare methods."""

    # Index hourly data by (date_str, hour)
    hourly_by_dt: dict[datetime, float] = {}
    for h in all_hourly:
        hourly_by_dt[h["dt"]] = h["m3"]

    # Get all complete days
    days_data: dict[str, list[dict[str, Any]]] = {}
    for h in all_hourly:
        day_key = h["dt"].strftime("%Y-%m-%d")
        days_data.setdefault(day_key, []).append(h)

    complete_days = sorted(
        day for day, hours in days_data.items()
        if len(hours) >= MIN_COMPLETE_DAY_HOURS
    )

    if len(complete_days) < 32:
        print(f"Only {len(complete_days)} complete days — need at least 32 for meaningful backtest")
        return

    print(f"\nData range: {complete_days[0]} to {complete_days[-1]}")
    print(f"Complete days: {len(complete_days)}")
    print(f"Total hourly records: {len(all_hourly)}")

    # Test configuration
    gap_lengths = [6, 12, 18, 24]
    gap_starts_utc = [0, 4, 6, 8, 12, 16, 18, 22]  # various start hours
    training_window = 28  # days

    # Results storage
    results: list[dict[str, Any]] = []

    # Walk through test days (need 30 days before for training)
    test_days = complete_days[30:]
    print(f"Test days: {len(test_days)} ({test_days[0]} to {test_days[-1]})")
    print(f"Training window: {training_window} days before each test day")
    print(f"Gap lengths: {gap_lengths}h")
    print(f"Gap start hours (UTC): {gap_starts_utc}")
    print("\nRunning backtest", end="", flush=True)

    for day_idx, test_day in enumerate(test_days):
        if day_idx % 5 == 0:
            print(".", end="", flush=True)

        test_dt = datetime.strptime(test_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        # Training data: preceding training_window days
        train_start = test_dt - timedelta(days=training_window)
        training_hourly = [
            h for h in all_hourly
            if train_start <= h["dt"] < test_dt
        ]

        if len(training_hourly) < training_window * 18:  # need reasonable coverage
            continue

        # Predict daily m³ for test day
        predicted = predict_daily_m3(training_hourly, temperatures, test_day)
        if predicted is None or predicted <= 0:
            continue

        # Build profile from training data
        profile = build_hourly_profile(training_hourly)

        # Actual daily total
        actual_daily = sum(h["m3"] for h in days_data.get(test_day, []))

        for gap_len in gap_lengths:
            for gap_start in gap_starts_utc:
                # Actual consumption for this gap window
                actual_gap = 0.0
                hours_found = 0
                for i in range(gap_len):
                    hour = (gap_start + i) % 24
                    # If gap crosses midnight, it goes into next day
                    day_offset = (gap_start + i) // 24
                    gap_dt = test_dt + timedelta(hours=gap_start + i)
                    if gap_dt in hourly_by_dt:
                        actual_gap += hourly_by_dt[gap_dt]
                        hours_found += 1

                if hours_found < gap_len * 0.8:  # need 80% of hours
                    continue

                # Flat estimation
                flat_est = predicted * (gap_len / 24)

                # Profile estimation
                profile_weight = sum(profile[(gap_start + i) % 24] for i in range(gap_len))
                profile_est = predicted * profile_weight

                results.append({
                    "day": test_day,
                    "month": test_day[:7],
                    "gap_start": gap_start,
                    "gap_len": gap_len,
                    "actual": actual_gap,
                    "flat_est": flat_est,
                    "profile_est": profile_est,
                    "flat_err": flat_est - actual_gap,
                    "profile_err": profile_est - actual_gap,
                    "actual_daily": actual_daily,
                    "predicted_daily": predicted,
                })

    print(f" done! ({len(results)} test cases)\n")

    if not results:
        print("No valid test cases generated.")
        return

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------

    def metrics(errors: list[float], actuals: list[float]) -> dict[str, float]:
        n = len(errors)
        mae = sum(abs(e) for e in errors) / n
        rmse = math.sqrt(sum(e * e for e in errors) / n)
        mape_vals = [abs(e) / a * 100 for e, a in zip(errors, actuals) if a > 0.5]
        mape = sum(mape_vals) / len(mape_vals) if mape_vals else 0
        return {"MAE": mae, "RMSE": rmse, "MAPE": mape}

    def print_metrics_row(label: str, flat_m: dict, prof_m: dict) -> None:
        def winner(key: str) -> str:
            if prof_m[key] < flat_m[key] - 0.01:
                return " <--"
            return ""
        print(f"  {label:<30s}  "
              f"MAE {flat_m['MAE']:6.1f} vs {prof_m['MAE']:6.1f}{winner('MAE'):4s}  "
              f"MAPE {flat_m['MAPE']:5.1f}% vs {prof_m['MAPE']:5.1f}%{winner('MAPE'):4s}  "
              f"RMSE {flat_m['RMSE']:6.1f} vs {prof_m['RMSE']:6.1f}{winner('RMSE'):4s}")

    # Overall
    print("=" * 100)
    print("OVERALL RESULTS")
    print("=" * 100)
    flat_errs = [r["flat_err"] for r in results]
    prof_errs = [r["profile_err"] for r in results]
    actuals = [r["actual"] for r in results]
    fm = metrics(flat_errs, actuals)
    pm = metrics(prof_errs, actuals)
    print(f"  {'Method':<20s} {'MAE (m3)':>10s} {'MAPE (%)':>10s} {'RMSE (m3)':>10s}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'Flat (/24)':<20s} {fm['MAE']:10.2f} {fm['MAPE']:10.1f} {fm['RMSE']:10.2f}")
    print(f"  {'Hourly Profile':<20s} {pm['MAE']:10.2f} {pm['MAPE']:10.1f} {pm['RMSE']:10.2f}")
    improvement = (fm['MAE'] - pm['MAE']) / fm['MAE'] * 100 if fm['MAE'] > 0 else 0
    print(f"\n  Profile MAE improvement: {improvement:+.1f}%")

    # By gap length
    print(f"\n{'='*100}")
    print("BY GAP LENGTH                        Flat          vs    Profile")
    print("=" * 100)
    for gl in gap_lengths:
        subset = [r for r in results if r["gap_len"] == gl]
        if not subset:
            continue
        fm = metrics([r["flat_err"] for r in subset], [r["actual"] for r in subset])
        pm = metrics([r["profile_err"] for r in subset], [r["actual"] for r in subset])
        print_metrics_row(f"Gap {gl}h ({len(subset)} cases)", fm, pm)

    # By gap start hour
    print(f"\n{'='*100}")
    print("BY GAP START HOUR (UTC)              Flat          vs    Profile")
    print("=" * 100)
    for gs in gap_starts_utc:
        subset = [r for r in results if r["gap_start"] == gs]
        if not subset:
            continue
        fm = metrics([r["flat_err"] for r in subset], [r["actual"] for r in subset])
        pm = metrics([r["profile_err"] for r in subset], [r["actual"] for r in subset])
        label_local = f"UTC {gs:02d} (~local {(gs+2)%24:02d})"
        print_metrics_row(f"Start {label_local} ({len(subset)})", fm, pm)

    # By month
    print(f"\n{'='*100}")
    print("BY MONTH                             Flat          vs    Profile")
    print("=" * 100)
    months = sorted(set(r["month"] for r in results))
    for m in months:
        subset = [r for r in results if r["month"] == m]
        fm = metrics([r["flat_err"] for r in subset], [r["actual"] for r in subset])
        pm = metrics([r["profile_err"] for r in subset], [r["actual"] for r in subset])
        print_metrics_row(f"{m} ({len(subset)} cases)", fm, pm)

    # By gap start + gap length combination (most interesting)
    print(f"\n{'='*100}")
    print("NIGHT vs DAY GAPS (most relevant)")
    print("=" * 100)
    night_gaps = [r for r in results if r["gap_start"] in (22, 0, 4)]
    day_gaps = [r for r in results if r["gap_start"] in (8, 12, 16)]
    if night_gaps:
        fm = metrics([r["flat_err"] for r in night_gaps], [r["actual"] for r in night_gaps])
        pm = metrics([r["profile_err"] for r in night_gaps], [r["actual"] for r in night_gaps])
        print_metrics_row(f"Night starts ({len(night_gaps)})", fm, pm)
    if day_gaps:
        fm = metrics([r["flat_err"] for r in day_gaps], [r["actual"] for r in day_gaps])
        pm = metrics([r["profile_err"] for r in day_gaps], [r["actual"] for r in day_gaps])
        print_metrics_row(f"Day starts ({len(day_gaps)})", fm, pm)

    # Daily prediction accuracy (separate from gap method)
    print(f"\n{'='*100}")
    print("DAILY PREDICTION ACCURACY (regression model)")
    print("=" * 100)
    seen_days = set()
    daily_errs = []
    for r in results:
        if r["day"] not in seen_days:
            seen_days.add(r["day"])
            daily_errs.append({
                "day": r["day"],
                "actual": r["actual_daily"],
                "predicted": r["predicted_daily"],
                "err": r["predicted_daily"] - r["actual_daily"],
            })
    if daily_errs:
        dm = metrics(
            [d["err"] for d in daily_errs],
            [d["actual"] for d in daily_errs],
        )
        print(f"  Days tested: {len(daily_errs)}")
        print(f"  MAE:  {dm['MAE']:.1f} m3/day")
        print(f"  MAPE: {dm['MAPE']:.1f}%")
        print(f"  RMSE: {dm['RMSE']:.1f} m3/day")

    # Win/loss summary
    print(f"\n{'='*100}")
    print("WIN/LOSS SUMMARY (profile vs flat, per test case)")
    print("=" * 100)
    profile_wins = sum(1 for r in results if abs(r["profile_err"]) < abs(r["flat_err"]))
    flat_wins = sum(1 for r in results if abs(r["flat_err"]) < abs(r["profile_err"]))
    ties = len(results) - profile_wins - flat_wins
    print(f"  Profile wins: {profile_wins} ({profile_wins/len(results)*100:.0f}%)")
    print(f"  Flat wins:    {flat_wins} ({flat_wins/len(results)*100:.0f}%)")
    print(f"  Ties:         {ties} ({ties/len(results)*100:.0f}%)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print("Gas Gap Estimation Backtest: Hourly Profile vs Flat")
    print("=" * 55)

    if len(sys.argv) >= 3:
        client_id = sys.argv[1]
        client_secret = sys.argv[2]
    else:
        client_id = input("Enter client_id: ").strip()
        client_secret = getpass("Enter client_secret: ").strip()

    async with aiohttp.ClientSession() as session:
        # Auth
        print("\nAuthenticating...", end=" ", flush=True)
        token = await fetch_token(session, client_id, client_secret)
        print("OK")

        # Fetch gas data in 30-day chunks (going back ~90 days)
        now = datetime.now(timezone.utc)
        all_hourly: list[dict[str, Any]] = []
        seen_dts: set[datetime] = set()

        chunks = [
            (now - timedelta(days=90), now - timedelta(days=60)),
            (now - timedelta(days=60), now - timedelta(days=30)),
            (now - timedelta(days=30), now),
        ]

        for i, (start, end) in enumerate(chunks):
            print(f"Fetching gas data chunk {i+1}/3 ({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})...",
                  end=" ", flush=True)
            if i > 0:
                await asyncio.sleep(7)  # Rate limit
            chunk_data = await fetch_gas_data(session, token, start, end)
            new = 0
            for h in chunk_data:
                if h["dt"] not in seen_dts:
                    seen_dts.add(h["dt"])
                    all_hourly.append(h)
                    new += 1
            print(f"{new} new records")

        all_hourly.sort(key=lambda h: h["dt"])
        print(f"\nTotal: {len(all_hourly)} hourly records")
        if all_hourly:
            print(f"Range: {all_hourly[0]['dt'].strftime('%Y-%m-%d')} to {all_hourly[-1]['dt'].strftime('%Y-%m-%d')}")

        # Fetch weather
        print("\nFetching weather data (92 days)...", end=" ", flush=True)
        temperatures = await fetch_weather(session, past_days=92)
        print(f"{len(temperatures)} hourly temperatures")

    # Run backtest
    run_backtest(all_hourly, temperatures)


if __name__ == "__main__":
    asyncio.run(main())
