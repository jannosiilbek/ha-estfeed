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
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Any

import aiohttp

from test_utils import (
    MIN_COMPLETE_DAY_HOURS,
    THERMAL_WEIGHTS,
    build_hourly_profile,
    daily_avg_temp,
    fetch_gas_data_parsed,
    fetch_token,
    fetch_weather,
    linear_regression,
)

EIC = "38ZEE-G0120307-8"


def predict_daily_m3(
    training_data: list[dict[str, Any]],
    temperatures: dict[datetime, float],
    target_day: str,
) -> float | None:
    """Run thermal-inertia-aware regression on training data, predict for target_day."""
    daily: dict[str, dict[str, float]] = {}
    for h in training_data:
        day_key = h["dt"].strftime("%Y-%m-%d")
        if day_key not in daily:
            daily[day_key] = {"m3": 0.0, "hours": 0}
        daily[day_key]["m3"] += h["m3"]
        daily[day_key]["hours"] += 1

    daily_temps: dict[str, float] = {}
    for day_key, agg in daily.items():
        if agg["hours"] < MIN_COMPLETE_DAY_HOURS:
            continue
        t = daily_avg_temp(day_key, temperatures)
        if t is not None:
            daily_temps[day_key] = t

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
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(
    all_hourly: list[dict[str, Any]],
    temperatures: dict[datetime, float],
) -> None:
    """Walk through historical data, simulate gaps, compare methods."""

    hourly_by_dt: dict[datetime, float] = {}
    for h in all_hourly:
        hourly_by_dt[h["dt"]] = h["m3"]

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

    gap_lengths = [6, 12, 18, 24]
    gap_starts_utc = [0, 4, 6, 8, 12, 16, 18, 22]
    training_window = 28

    results: list[dict[str, Any]] = []

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

        train_start = test_dt - timedelta(days=training_window)
        training_hourly = [
            h for h in all_hourly
            if train_start <= h["dt"] < test_dt
        ]

        if len(training_hourly) < training_window * 18:
            continue

        predicted = predict_daily_m3(training_hourly, temperatures, test_day)
        if predicted is None or predicted <= 0:
            continue

        profile = build_hourly_profile(training_hourly)
        actual_daily = sum(h["m3"] for h in days_data.get(test_day, []))

        for gap_len in gap_lengths:
            for gap_start in gap_starts_utc:
                actual_gap = 0.0
                hours_found = 0
                for i in range(gap_len):
                    gap_dt = test_dt + timedelta(hours=gap_start + i)
                    if gap_dt in hourly_by_dt:
                        actual_gap += hourly_by_dt[gap_dt]
                        hours_found += 1

                if hours_found < gap_len * 0.8:
                    continue

                flat_est = predicted * (gap_len / 24)
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
        print("\nAuthenticating...", end=" ", flush=True)
        token = await fetch_token(session, client_id, client_secret)
        print("OK")

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
            chunk_data = await fetch_gas_data_parsed(session, token, start, end, EIC)
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

        print("\nFetching weather data (92 days)...", end=" ", flush=True)
        temperatures = await fetch_weather(session, past_days=92)
        print(f"{len(temperatures)} hourly temperatures")

    run_backtest(all_hourly, temperatures)


if __name__ == "__main__":
    asyncio.run(main())
