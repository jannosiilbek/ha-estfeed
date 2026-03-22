"""
Test all Estfeed integration sensors with live API data.

Shows every sensor exactly as Home Assistant would see it,
with full metadata: device_class, state_class, unit, precision,
extra attributes, device info, and enabled status.

Usage:
  python3 test_sensors.py                          # uses .env credentials
  python3 test_sensors.py <client_id> <secret>     # inline credentials
  python3 test_sensors.py --prices-only            # skip gas (no auth needed)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

import aiohttp

# -- Constants (mirrors const.py) ------------------------------------------

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
GAS_PRICE_URL = "https://dashboard.elering.ee/api/gas-trade"
ELECTRICITY_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_CALORIFIC_KWH_M3 = 10.6
THERMAL_WEIGHTS = (0.40, 0.40, 0.20)
MIN_COMPLETE_DAY_HOURS = 20
LAT, LON = 59.437, 24.7536

# -- ANSI helpers -----------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
WHITE = "\033[97m"
BLUE = "\033[34m"


def header(text: str) -> str:
    return f"\n{BOLD}{CYAN}{'━' * 70}{RESET}\n{BOLD}{WHITE}  {text}{RESET}\n{BOLD}{CYAN}{'━' * 70}{RESET}"


def device_header(name: str, manufacturer: str, model: str) -> str:
    return (
        f"\n{BOLD}{MAGENTA}┌─ Device: {name}{RESET}\n"
        f"{DIM}│  Manufacturer: {manufacturer}   Model: {model}   Type: SERVICE{RESET}"
    )


def sensor_block(
    key: str,
    value: Any,
    *,
    device_name_slug: str,
    unit: str | None = None,
    device_class: str | None = None,
    state_class: str | None = None,
    precision: int | None = None,
    enabled: bool = True,
    available: bool = True,
    translation_key: str | None = None,
    attributes: dict[str, Any] | None = None,
    last: bool = False,
) -> str:
    connector = "└" if last else "├"
    vline = " " if last else "│"

    # HA entity_id and unique_id
    entity_id = f"sensor.{device_name_slug}_{key}"
    friendly_name = f"{device_name_slug.replace('_', ' ').title()} {key.replace('_', ' ')}"

    # Format value
    if not available:
        val_str = f"{YELLOW}unavailable{RESET}"
    elif value is None:
        val_str = f"{DIM}None{RESET}"
    elif isinstance(value, bool):
        val_str = f"{GREEN}{value}{RESET}"
    elif isinstance(value, float) and precision is not None:
        val_str = f"{GREEN}{value:.{precision}f}{RESET}"
    else:
        val_str = f"{GREEN}{value}{RESET}"

    unit_str = f" {YELLOW}{unit}{RESET}" if unit else ""

    lines = [f"{BOLD}│{RESET}"]
    lines.append(
        f"{BOLD}│{RESET}  {connector}─ {BOLD}{WHITE}{key}{RESET}"
    )

    # State line
    lines.append(
        f"{BOLD}│{RESET}  {vline}     state: {val_str}{unit_str}"
    )

    # Entity ID
    lines.append(
        f"{BOLD}│{RESET}  {vline}     {DIM}entity_id:      {CYAN}{entity_id}{RESET}"
    )

    # Friendly name
    lines.append(
        f"{BOLD}│{RESET}  {vline}     {DIM}friendly_name:  {friendly_name}{RESET}"
    )

    # unique_id
    lines.append(
        f"{BOLD}│{RESET}  {vline}     {DIM}unique_id:      {{entry_id}}_{key}{RESET}"
    )

    # translation_key
    if translation_key:
        lines.append(
            f"{BOLD}│{RESET}  {vline}     {DIM}translation_key: {translation_key}{RESET}"
        )

    # available
    avail_color = GREEN if available else YELLOW
    lines.append(
        f"{BOLD}│{RESET}  {vline}     {DIM}available:      {avail_color}{available}{RESET}"
    )

    # enabled
    if not enabled:
        lines.append(
            f"{BOLD}│{RESET}  {vline}     {DIM}enabled_default: {YELLOW}False{RESET}"
        )

    # has_entity_name
    lines.append(
        f"{BOLD}│{RESET}  {vline}     {DIM}has_entity_name: True{RESET}"
    )

    # Metadata line
    meta_parts = []
    if device_class:
        meta_parts.append(f"device_class: {BLUE}{device_class}{RESET}")
    else:
        meta_parts.append(f"device_class: {DIM}None{RESET}")
    if state_class:
        meta_parts.append(f"state_class: {BLUE}{state_class}{RESET}")
    else:
        meta_parts.append(f"state_class: {DIM}None{RESET}")
    if precision is not None:
        meta_parts.append(f"precision: {BLUE}{precision}{RESET}")

    lines.append(f"{BOLD}│{RESET}  {vline}     {DIM}{'  │  '.join(meta_parts)}{RESET}")

    # Unit
    if unit:
        lines.append(
            f"{BOLD}│{RESET}  {vline}     {DIM}unit_of_measurement: {YELLOW}{unit}{RESET}"
        )

    # Extra state attributes
    if attributes:
        lines.append(f"{BOLD}│{RESET}  {vline}     {DIM}extra_state_attributes:{RESET}")
        for attr_key, attr_val in attributes.items():
            if isinstance(attr_val, list) and len(attr_val) > 3:
                lines.append(
                    f"{BOLD}│{RESET}  {vline}       {DIM}{attr_key}: "
                    f"[{len(attr_val)} entries] first 3:{RESET}"
                )
                for item in attr_val[:3]:
                    lines.append(f"{BOLD}│{RESET}  {vline}         {DIM}{item}{RESET}")
            elif isinstance(attr_val, list):
                lines.append(
                    f"{BOLD}│{RESET}  {vline}       {DIM}{attr_key}: {attr_val}{RESET}"
                )
            else:
                lines.append(
                    f"{BOLD}│{RESET}  {vline}       {DIM}{attr_key}: {attr_val}{RESET}"
                )

    return "\n".join(lines)


# -- API fetch functions ----------------------------------------------------

async def fetch_token(
    session: aiohttp.ClientSession, client_id: str, client_secret: str
) -> str:
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
    eics: list[str],
) -> list[dict[str, Any]]:
    await asyncio.sleep(6)  # rate limit
    params = {
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_hour",
        "meteringPointEics": ",".join(eics),
    }
    async with session.get(
        f"{BASE_URL}/api/public/v1/metering-data",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Metering data: {resp.status}")
        return await resp.json()


async def fetch_gas_price(
    session: aiohttp.ClientSession,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    params = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    async with session.get(GAS_PRICE_URL, params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        if not data.get("success"):
            return None
        entries = [
            e for e in data.get("data", {}).get("common", [])
            if e.get("price") is not None
        ]
        if not entries:
            return None
        latest = max(entries, key=lambda e: e["timestamp"])
        price_eur_mwh = latest["price"]
        price_dt = datetime.fromtimestamp(latest["timestamp"], tz=timezone.utc)
        return {
            "price_eur_mwh": round(price_eur_mwh, 3),
            "price_eur_kwh": round(price_eur_mwh / 1000, 6),
            "price_date": price_dt.strftime("%Y-%m-%d"),
        }


async def fetch_electricity_prices(
    session: aiohttp.ClientSession,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = today_start + timedelta(days=2)
    params = {
        "start": today_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end": tomorrow_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    async with session.get(ELECTRICITY_PRICE_URL, params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        if not data.get("success"):
            return None
        entries = [
            e for e in data.get("data", {}).get("ee", [])
            if e.get("price") is not None
        ]
        if not entries:
            return None

    # Partition today/tomorrow
    tomorrow_start_ts = int((today_start + timedelta(days=1)).timestamp())
    today_entries = [e for e in entries if e["timestamp"] < tomorrow_start_ts]
    tomorrow_entries = [e for e in entries if e["timestamp"] >= tomorrow_start_ts]

    now_ts = int(now.timestamp())
    current_entry = None
    for e in sorted(today_entries, key=lambda x: x["timestamp"]):
        if e["timestamp"] <= now_ts:
            current_entry = e
        else:
            break

    current_price = current_entry["price"] if current_entry else today_entries[0]["price"]
    today_prices = [e["price"] for e in today_entries]

    # Next hour
    next_hour_ts = now_ts + 3600
    next_hour_price = None
    for e in sorted(entries, key=lambda x: x["timestamp"]):
        if e["timestamp"] <= next_hour_ts:
            next_hour_price = e["price"]
        else:
            break

    def _fmt(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "start": datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).isoformat(),
                "price_eur_kwh": round(e["price"] / 1000, 6),
            }
            for e in sorted(raw, key=lambda x: x["timestamp"])
        ]

    return {
        "current_price_eur_kwh": round(current_price / 1000, 6),
        "today_avg_eur_kwh": round(sum(today_prices) / len(today_prices) / 1000, 6),
        "today_min_eur_kwh": round(min(today_prices) / 1000, 6),
        "today_max_eur_kwh": round(max(today_prices) / 1000, 6),
        "next_hour_eur_kwh": round(next_hour_price / 1000, 6) if next_hour_price else None,
        "prices_today": _fmt(today_entries),
        "prices_tomorrow": _fmt(tomorrow_entries),
    }


async def fetch_weather(session: aiohttp.ClientSession) -> dict[datetime, float]:
    params = {
        "latitude": LAT, "longitude": LON,
        "hourly": "temperature_2m",
        "past_days": 31, "forecast_days": 1,
        "timeformat": "iso8601", "timezone": "UTC",
    }
    async with session.get(OPEN_METEO_URL, params=params) as resp:
        if resp.status != 200:
            return {}
        data = await resp.json()
    times = data.get("hourly", {}).get("time", [])
    temps = data.get("hourly", {}).get("temperature_2m", [])
    return {
        datetime.fromisoformat(t).replace(tzinfo=timezone.utc): temp
        for t, temp in zip(times, temps) if temp is not None
    }


# -- Gas data processing (mirrors coordinator logic) -----------------------

def parse_hourly_gas(
    metering_data: list[dict[str, Any]], gas_eics: list[str]
) -> list[dict[str, Any]]:
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


def compute_calorific(hourly: list[dict[str, Any]], now: datetime) -> float:
    cutoff = now - timedelta(hours=48)
    total_kwh = sum(h["kwh"] for h in hourly if h["dt"] >= cutoff and h["m3"] > 0)
    total_m3 = sum(h["m3"] for h in hourly if h["dt"] >= cutoff and h["m3"] > 0)
    return total_kwh / total_m3 if total_m3 > 0 else DEFAULT_CALORIFIC_KWH_M3


def build_hourly_profile(hourly: list[dict[str, Any]]) -> list[float]:
    days: dict[str, list[dict[str, Any]]] = {}
    for h in hourly:
        days.setdefault(h["dt"].strftime("%Y-%m-%d"), []).append(h)
    hour_sums = [0.0] * 24
    count = 0
    for hours in days.values():
        if len(hours) < MIN_COMPLETE_DAY_HOURS:
            continue
        total = sum(h["m3"] for h in hours)
        if total <= 0:
            continue
        count += 1
        for h in hours:
            hour_sums[h["dt"].hour] += h["m3"] / total
    if count == 0:
        return [1.0 / 24] * 24
    profile = [s / count for s in hour_sums]
    ps = sum(profile)
    return [p / ps for p in profile] if ps > 0 else [1.0 / 24] * 24


def linear_regression(x: list[float], y: list[float]) -> tuple[float, float] | None:
    n = len(x)
    if n < 3 or len(y) != n:
        return None
    sx, sy = sum(x), sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    d = n * sxx - sx * sx
    if abs(d) < 1e-10:
        return None
    return (n * sxy - sx * sy) / d, (sy * sxx - sx * sxy) / d


def daily_avg_temp(day: str, temps: dict[datetime, float]) -> float | None:
    vals = [
        temps[datetime.strptime(day, "%Y-%m-%d").replace(hour=h, tzinfo=timezone.utc)]
        for h in range(24)
        if datetime.strptime(day, "%Y-%m-%d").replace(hour=h, tzinfo=timezone.utc) in temps
    ]
    return sum(vals) / len(vals) if vals else None


def estimate_gap(
    hourly: list[dict[str, Any]],
    temps: dict[datetime, float],
    last_actual: datetime,
    now: datetime,
) -> tuple[float, float, list[float]]:
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
        wt.append(w0 * daily_temps[sorted_days[i]] + w1 * daily_temps[sorted_days[i-1]] + w2 * daily_temps[sorted_days[i-2]])
        dm.append(daily[sorted_days[i]]["m3"])

    profile = build_hourly_profile(hourly)
    reg = linear_regression(wt, dm)
    if reg is None:
        return 0.0, 0.0, profile

    slope, intercept = reg
    gap_hours = max(1, int((now - last_actual).total_seconds() / 3600))
    gap_temps = [temps[dt] for i in range(gap_hours)
                 if (dt := (last_actual + timedelta(hours=i+1)).replace(minute=0, second=0, microsecond=0)) in temps]
    if not gap_temps:
        return 0.0, 0.0, profile

    today_avg = sum(gap_temps) / len(gap_temps)
    yest = daily_avg_temp((now - timedelta(days=1)).strftime("%Y-%m-%d"), temps) or today_avg
    d2 = daily_avg_temp((now - timedelta(days=2)).strftime("%Y-%m-%d"), temps) or yest
    weighted = w0 * today_avg + w1 * yest + w2 * d2
    pred_daily = max(0, slope * weighted + intercept)

    gap_weight = sum(
        profile[(last_actual + timedelta(hours=i+1)).replace(minute=0, second=0, microsecond=0).hour]
        for i in range(gap_hours)
    )
    return round(pred_daily * gap_weight, 2), round(pred_daily, 2), profile


def process_gas_data(
    metering_data: list[dict[str, Any]],
    gas_eics: list[str],
    temperatures: dict[datetime, float],
    apartment_area: float,
    building_area: float,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    hourly = parse_hourly_gas(metering_data, gas_eics)
    if not hourly:
        return {"has_gas": False, "area_ratio": 0, "is_estimated": False,
                "gas": {"apartment_total_m3": 0, "apartment_total_kwh": 0,
                        "apartment_today_m3": 0, "apartment_flow_rate_m3h": 0}}

    month_actual_m3 = sum(h["m3"] for h in hourly if h["dt"] >= month_start)
    month_actual_kwh = sum(h["kwh"] for h in hourly if h["dt"] >= month_start)
    today_actual_m3 = sum(h["m3"] for h in hourly if h["dt"] >= today_start)
    last_actual_dt = max((h["dt"] for h in hourly if h["m3"] > 0 or h["kwh"] > 0), default=None)
    calorific = compute_calorific(hourly, now)

    estimated_m3 = 0.0
    predicted_daily = 0.0
    profile = None
    is_estimated = False
    if last_actual_dt and last_actual_dt < now - timedelta(hours=1):
        estimated_m3, predicted_daily, profile = estimate_gap(hourly, temperatures, last_actual_dt, now)
        is_estimated = estimated_m3 > 0

    total_m3 = month_actual_m3 + estimated_m3
    total_kwh = month_actual_kwh + estimated_m3 * calorific
    today_m3 = today_actual_m3
    if last_actual_dt and predicted_daily > 0 and profile is not None:
        gap_h = max(1, int((now - last_actual_dt).total_seconds() / 3600))
        today_gap_weight = 0.0
        for i in range(gap_h):
            gap_dt = (last_actual_dt + timedelta(hours=i + 1)).replace(
                minute=0, second=0, microsecond=0
            )
            if gap_dt >= today_start:
                today_gap_weight += profile[gap_dt.hour]
        today_m3 += predicted_daily * today_gap_weight

    area_ratio = apartment_area / building_area if building_area > 0 else 0
    flow_rate = predicted_daily * profile[now.hour] * area_ratio if predicted_daily > 0 and profile else 0
    gap_hours = int((now - last_actual_dt).total_seconds() / 3600) if last_actual_dt else 0

    return {
        "has_gas": True,
        "area_ratio": area_ratio,
        "is_estimated": is_estimated,
        "gas": {
            "apartment_total_m3": round(total_m3 * area_ratio, 2),
            "apartment_total_kwh": round(total_kwh * area_ratio, 2),
            "apartment_today_m3": round(today_m3 * area_ratio, 2),
            "apartment_flow_rate_m3h": round(flow_rate, 3),
        },
        # Diagnostics (not exposed as sensors, but useful for debugging)
        "_diag": {
            "building_total_m3": round(total_m3, 2),
            "building_total_kwh": round(total_kwh, 2),
            "month_actual_m3": round(month_actual_m3, 2),
            "month_actual_kwh": round(month_actual_kwh, 2),
            "estimated_gap_m3": round(estimated_m3, 2),
            "predicted_daily_m3": round(predicted_daily, 2),
            "calorific_kwh_per_m3": round(calorific, 2),
            "gap_hours": gap_hours,
            "last_actual_dt": last_actual_dt.isoformat() if last_actual_dt else None,
            "today_actual_m3": round(today_actual_m3, 2),
            "hourly_records": len(hourly),
        },
    }


# -- Main ------------------------------------------------------------------

async def main() -> None:
    prices_only = "--prices-only" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    # Load .env file if present
    env_file = Path(__file__).parent / ".env"
    env_vars: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

    client_id = client_secret = ""
    apartment_area = building_area = 0.0

    if not prices_only:
        if len(args) >= 2:
            client_id, client_secret = args[0], args[1]
        elif "ESTFEED_CLIENT_ID" in env_vars:
            client_id = env_vars["ESTFEED_CLIENT_ID"]
            client_secret = env_vars["ESTFEED_CLIENT_SECRET"]
            print(f"Using credentials from .env")
        else:
            client_id = input("Client ID: ").strip()
            client_secret = getpass("Client secret: ").strip()
        apartment_area = float(input("Apartment area m² [54.4]: ").strip() or "54.4")
        building_area = float(input("Building area m² [816.6]: ").strip() or "816.6")

    print(header("Estfeed Integration — Sensor Preview"))
    print(f"{DIM}  Fetching live data from APIs...{RESET}")

    now = datetime.now(timezone.utc)
    gas_data = None
    gas_eics: list[str] = []

    async with aiohttp.ClientSession() as session:
        # Fetch all data sources in parallel where possible
        tasks: dict[str, Any] = {}
        tasks["gas_price"] = asyncio.create_task(fetch_gas_price(session))
        tasks["elec_price"] = asyncio.create_task(fetch_electricity_prices(session))

        if not prices_only:
            token = await fetch_token(session, client_id, client_secret)
            print(f"{DIM}  ✓ Authenticated{RESET}")

            metering_points = await fetch_metering_points(
                session, token, now - timedelta(days=30), now
            )
            gas_eics = [
                mp["eic"] for mp in metering_points
                if mp["commodityType"] == "NATURAL_GAS"
            ]
            print(f"{DIM}  ✓ Found {len(gas_eics)} gas metering point(s): {', '.join(gas_eics)}{RESET}")

            if gas_eics:
                gas_raw = await fetch_gas_data(
                    session, token, now - timedelta(days=31), now, gas_eics
                )
                temperatures = await fetch_weather(session)
                gas_data = process_gas_data(
                    gas_raw, gas_eics, temperatures, apartment_area, building_area
                )
                print(f"{DIM}  ✓ Gas data processed{RESET}")

        gas_price = await tasks["gas_price"]
        elec_price = await tasks["elec_price"]
        print(f"{DIM}  ✓ Price data fetched{RESET}")

    # ── Display sensors ───────────────────────────────────────────────

    # 1. Apartment Gas sensors
    gas_available = bool(gas_data and gas_data["has_gas"])
    if gas_data or not prices_only:
        print(device_header("Apartment Gas", "Elering", "Estfeed"))
        gas_vals = gas_data if gas_data else {}
        gas_inner = gas_vals.get("gas", {})
        sensors = [
            ("apartment_gas_total", gas_inner.get("apartment_total_m3"),
             "m³", "gas", "total_increasing", 2, True, "apartment_gas_total"),
            ("apartment_gas_energy_total", gas_inner.get("apartment_total_kwh"),
             "kWh", "energy", "total_increasing", 2, True, "apartment_gas_energy_total"),
            ("apartment_gas_today", gas_inner.get("apartment_today_m3"),
             "m³", "gas", "total", 2, True, "apartment_gas_today"),
            ("apartment_gas_flow_rate", gas_inner.get("apartment_flow_rate_m3h"),
             "m³/h", "volume_flow_rate", "measurement", 3, True, "apartment_gas_flow_rate"),
            ("apartment_gas_estimated", gas_vals.get("is_estimated"),
             None, None, None, None, False, "apartment_gas_estimated"),
        ]
        for i, (key, val, unit, dc, sc, prec, enabled, tkey) in enumerate(sensors):
            print(sensor_block(
                key, val,
                device_name_slug="apartment_gas",
                unit=unit, device_class=dc, state_class=sc,
                precision=prec, enabled=enabled,
                available=gas_available,
                translation_key=tkey,
                last=(i == len(sensors) - 1),
            ))

        # Coordinator diagnostics
        if gas_data and "_diag" in gas_data:
            diag = gas_data["_diag"]
            print(f"{BOLD}│{RESET}")
            print(f"{BOLD}│{RESET}  {CYAN}Coordinator Diagnostics{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}{'─' * 50}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}area_ratio:           {gas_data['area_ratio']:.4f} ({apartment_area}/{building_area} m²){RESET}")
            print(f"{BOLD}│{RESET}  {DIM}building_total_m3:    {diag['building_total_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}building_total_kwh:   {diag['building_total_kwh']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}month_actual_m3:      {diag['month_actual_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}month_actual_kwh:     {diag['month_actual_kwh']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}estimated_gap_m3:     {diag['estimated_gap_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}predicted_daily_m3:   {diag['predicted_daily_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}calorific_kwh_per_m3: {diag['calorific_kwh_per_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}gap_hours:            {diag['gap_hours']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}last_actual_dt:       {diag['last_actual_dt']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}today_actual_m3:      {diag['today_actual_m3']}{RESET}")
            print(f"{BOLD}│{RESET}  {DIM}hourly_records:       {diag['hourly_records']}{RESET}")

    # 2. Gas Market Price sensor
    print(device_header("Gas Market Price", "Elering", "GET Baltic"))
    if gas_price:
        print(sensor_block(
            "gas_market_price",
            gas_price["price_eur_kwh"],
            device_name_slug="gas_market_price",
            unit="EUR/kWh",
            state_class="measurement",
            precision=4,
            translation_key="gas_market_price",
            attributes={"price_date": gas_price["price_date"]},
            last=True,
        ))
    else:
        print(f"{BOLD}│{RESET}  {DIM}No gas price data available (unavailable){RESET}")

    # 3. Electricity Market Price sensors
    print(device_header("Electricity Market Price", "Elering", "Nord Pool EE"))
    if elec_price:
        e_sensors = [
            ("electricity_market_price", elec_price["current_price_eur_kwh"],
             "electricity_market_price",
             {"prices_today": elec_price["prices_today"],
              "prices_tomorrow": elec_price["prices_tomorrow"]}),
            ("electricity_price_today_avg", elec_price["today_avg_eur_kwh"],
             "electricity_price_today_avg", None),
            ("electricity_price_today_min", elec_price["today_min_eur_kwh"],
             "electricity_price_today_min", None),
            ("electricity_price_today_max", elec_price["today_max_eur_kwh"],
             "electricity_price_today_max", None),
            ("electricity_price_next_hour", elec_price["next_hour_eur_kwh"],
             "electricity_price_next_hour", None),
        ]
        for i, (key, val, tkey, attrs) in enumerate(e_sensors):
            print(sensor_block(
                key, val,
                device_name_slug="electricity_market_price",
                unit="EUR/kWh",
                state_class="measurement",
                precision=4,
                translation_key=tkey,
                attributes=attrs,
                last=(i == len(e_sensors) - 1),
            ))
    else:
        print(f"{BOLD}│{RESET}  {DIM}No electricity price data available (unavailable){RESET}")

    # Summary
    total_sensors = 11
    avail_count = (5 if gas_available else 0) + (1 if gas_price else 0) + (5 if elec_price else 0)
    print(f"\n{BOLD}{WHITE}  Summary{RESET}")
    print(f"{DIM}  {'─' * 40}{RESET}")
    print(f"  Sensors: {avail_count}/{total_sensors} available")
    print(f"  Update intervals:")
    print(f"    Gas data:         {DIM}3600s (1 hour){RESET}")
    print(f"    Gas price:        {DIM}3600s (1 hour){RESET}")
    print(f"    Electricity:      {DIM}900s (15 minutes){RESET}")
    print(f"  Timestamp: {DIM}{now.strftime('%Y-%m-%d %H:%M:%S UTC')}{RESET}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
