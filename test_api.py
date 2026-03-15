"""
Estfeed & Elering API validation script.
Run this to verify API access before building the HA integration.

Usage: python test_api.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Optional

import aiohttp

TOKEN_URL = "https://kc.elering.ee/realms/elering-sso/protocol/openid-connect/token"
BASE_URL = "https://estfeed.elering.ee"
ELERING_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"


async def test_estfeed_auth(session: aiohttp.ClientSession, client_id: str, client_secret: str) -> Optional[str]:
    """Test 1: Authenticate with Estfeed via Keycloak."""
    print("\n=== Test 1: Estfeed Authentication ===")
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
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return None
            data = await resp.json()
            token = data.get("access_token")
            expires_in = data.get("expires_in")
            scope = data.get("scope")
            print(f"  OK: Token received (expires in {expires_in}s, scope: {scope})")
            return token
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


async def test_metering_points(session: aiohttp.ClientSession, token: str) -> Optional[list[dict]]:
    """Test 2: Fetch metering point EICs."""
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
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return None
            data = await resp.json()
            print(f"  OK: Found {len(data)} metering point(s)")
            for mp in data:
                eic = mp.get("eic", "?")
                commodity = mp.get("commodityType", "?")
                periods = mp.get("periods", [])
                period_str = ", ".join(
                    f"{p.get('from', '?')} -> {p.get('to', 'ongoing')}" for p in periods
                )
                print(f"    - EIC: {eic} | Type: {commodity} | Periods: {period_str}")
            return data
    except Exception as e:
        print(f"  FAIL: {e}")
        return None


async def test_metering_data(session: aiohttp.ClientSession, token: str, eics: Optional[list[str]] = None) -> None:
    """Test 3: Fetch metering data for the last 7 days."""
    print("\n=== Test 3: Metering Data (last 7 days, daily resolution) ===")
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_day",
    }
    if eics:
        params["meteringPointEics"] = ",".join(eics)
    try:
        async with session.get(
            f"{BASE_URL}/api/public/v1/metering-data",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return
            data = await resp.json()
            print(f"  OK: Data for {len(data)} metering point(s)")
            for mp in data:
                eic = mp.get("meteringPointEic", "?")
                error = mp.get("error")
                if error:
                    print(f"    - EIC: {eic} | ERROR: {error}")
                    continue
                intervals = mp.get("accountingIntervals", [])
                print(f"    - EIC: {eic} | {len(intervals)} interval(s)")
                for interval in intervals:
                    period = interval.get("periodStart", "?")
                    kwh = interval.get("consumptionKwh")
                    prod_kwh = interval.get("productionKwh")
                    m3 = interval.get("consumptionM3")
                    parts = [f"period: {period}"]
                    if kwh is not None:
                        parts.append(f"consumption: {kwh} kWh")
                    if prod_kwh is not None:
                        parts.append(f"production: {prod_kwh} kWh")
                    if m3 is not None:
                        parts.append(f"consumption: {m3} m³")
                    print(f"      {' | '.join(parts)}")
    except Exception as e:
        print(f"  FAIL: {e}")


async def test_metering_data_hourly(session: aiohttp.ClientSession, token: str, eics: Optional[list[str]] = None) -> None:
    """Test 3b: Fetch metering data with hourly resolution for the last 24h."""
    print("\n=== Test 3b: Metering Data (last 24h, hourly resolution) ===")
    now = datetime.now(timezone.utc)
    params = {
        "startDateTime": (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resolution": "one_hour",
    }
    if eics:
        params["meteringPointEics"] = ",".join(eics)
    try:
        async with session.get(
            f"{BASE_URL}/api/public/v1/metering-data",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return
            data = await resp.json()
            print(f"  OK: Data for {len(data)} metering point(s)")
            for mp in data:
                eic = mp.get("meteringPointEic", "?")
                error = mp.get("error")
                if error:
                    print(f"    - EIC: {eic} | ERROR: {error}")
                    continue
                intervals = mp.get("accountingIntervals", [])
                print(f"    - EIC: {eic} | {len(intervals)} hourly interval(s)")
                for interval in intervals:
                    period = interval.get("periodStart", "?")
                    kwh = interval.get("consumptionKwh")
                    m3 = interval.get("consumptionM3")
                    parts = [f"period: {period}"]
                    if kwh is not None:
                        parts.append(f"consumption: {kwh} kWh")
                    if m3 is not None:
                        parts.append(f"consumption: {m3} m³ (≈ flow rate: {m3} m³/h)")
                    print(f"      {' | '.join(parts)}")
    except Exception as e:
        print(f"  FAIL: {e}")


async def test_current_price(session: aiohttp.ClientSession) -> None:
    """Test 4: Fetch current electricity spot price from Elering."""
    print("\n=== Test 4: Current Electricity Spot Price ===")
    try:
        async with session.get(f"{ELERING_PRICE_URL}/EE/current") as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return
            data = await resp.json()
            success = data.get("success")
            prices = data.get("data", [])
            if success and prices:
                price_mwh = prices[0].get("price")
                ts = prices[0].get("timestamp")
                price_kwh = price_mwh / 1000 if price_mwh is not None else None
                print(f"  OK: {price_mwh} EUR/MWh ({price_kwh:.4f} EUR/kWh) at timestamp {ts}")
            else:
                print(f"  WARN: Unexpected response: {data}")
    except Exception as e:
        print(f"  FAIL: {e}")


async def test_historical_prices(session: aiohttp.ClientSession) -> None:
    """Test 5: Fetch historical prices for last 24 hours."""
    print("\n=== Test 5: Historical Prices (last 24h) ===")
    now = datetime.now(timezone.utc)
    params = {
        "start": (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        async with session.get(ELERING_PRICE_URL, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"  FAIL: HTTP {resp.status} - {text}")
                return
            data = await resp.json()
            ee_prices = data.get("data", {}).get("ee", [])
            print(f"  OK: {len(ee_prices)} price interval(s) for Estonia")
            for p in ee_prices[:5]:
                ts = p.get("timestamp")
                price = p.get("price")
                dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else "?"
                print(f"    {dt} -> {price} EUR/MWh ({price/1000:.4f} EUR/kWh)")
            if len(ee_prices) > 5:
                print(f"    ... and {len(ee_prices) - 5} more")
    except Exception as e:
        print(f"  FAIL: {e}")


async def main():
    print("Estfeed & Elering API Validation")
    print("=" * 40)

    if len(sys.argv) >= 3:
        client_id = sys.argv[1]
        client_secret = sys.argv[2]
    else:
        client_id = input("Enter client_id: ").strip()
        client_secret = getpass("Enter client_secret: ").strip()

    if not client_id or not client_secret:
        print("Error: client_id and client_secret are required")
        print("Usage: python test_api.py <client_id> <client_secret>")
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        # Test 1: Auth
        token = await test_estfeed_auth(session, client_id, client_secret)
        if not token:
            print("\nAuthentication failed. Cannot continue with Estfeed tests.")
            print("Skipping to Elering price tests...\n")
        else:
            # Test 2: Metering points
            metering_points = await test_metering_points(session, token)

            # Rate limit pause
            print("\n  (waiting 5s for rate limit...)")
            await asyncio.sleep(5)

            # Test 3: Metering data
            eics = [mp["eic"] for mp in metering_points] if metering_points else None
            await test_metering_data(session, token, eics)

            # Rate limit pause
            print("\n  (waiting 5s for rate limit...)")
            await asyncio.sleep(5)

            # Test 3b: Hourly resolution metering data
            await test_metering_data_hourly(session, token, eics)

        # Test 4: Current price (public, no auth)
        await test_current_price(session)

        # Test 5: Historical prices
        await test_historical_prices(session)

    print("\n" + "=" * 40)
    print("Validation complete!")


if __name__ == "__main__":
    asyncio.run(main())
