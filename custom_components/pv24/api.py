"""API clients for PV24 metering data and Open-Meteo weather."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import (
    API_DATETIME_FORMAT,
    BASE_URL,
    ELECTRICITY_PRICE_URL,
    GAS_PRICE_URL,
    OPEN_METEO_URL,
    PRICE_API_DATETIME_FORMAT,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

_RATE_LIMIT_SECONDS = 6  # Estfeed API allows 1 request per 5s; add 1s buffer
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class EstfeedAuthError(Exception):
    """Authentication error."""


class EstfeedApiError(Exception):
    """General API error."""


class EstfeedApiClient:
    """Client for the Estfeed metering data API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0
        self._last_request_time: float = 0

    async def _check_response(
        self, resp: aiohttp.ClientResponse, context: str
    ) -> None:
        """Raise appropriate errors for non-200 responses."""
        if resp.status in (401, 403):
            raise EstfeedAuthError("Authentication failed")
        if resp.status != 200:
            text = await resp.text()
            raise EstfeedApiError(f"{context}: {resp.status} {text}")

    async def _throttle(self) -> None:
        """Ensure minimum interval between Estfeed API requests."""
        elapsed = time.monotonic() - self._last_request_time
        if self._last_request_time > 0 and elapsed < _RATE_LIMIT_SECONDS:
            delay = _RATE_LIMIT_SECONDS - elapsed
            _LOGGER.debug("Rate limiter: waiting %.1fs before next request", delay)
            await asyncio.sleep(delay)
        self._last_request_time = time.monotonic()

    async def _ensure_token(self) -> str:
        """Get a valid token, refreshing if expired."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        try:
            async with self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                await self._check_response(resp, "Token request failed")
                data = await resp.json()
                token: str = data["access_token"]
                self._token = token
                self._token_expiry = time.time() + data.get("expires_in", 300)
                return token
        except aiohttp.ClientError as err:
            raise EstfeedApiError(f"Connection error: {err}") from err

    async def authenticate(self) -> bool:
        """Validate credentials by fetching a token."""
        await self._ensure_token()
        return True

    async def get_metering_points(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Fetch metering point EICs linked to this API key."""
        token = await self._ensure_token()
        await self._throttle()
        params = {
            "startDateTime": start.strftime(API_DATETIME_FORMAT),
            "endDateTime": end.strftime(API_DATETIME_FORMAT),
        }
        try:
            async with self._session.get(
                f"{BASE_URL}/api/public/v1/metering-point-eics",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                await self._check_response(resp, "Metering points request failed")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise EstfeedApiError(f"Connection error: {err}") from err

    async def get_metering_data(
        self,
        start: datetime,
        end: datetime,
        resolution: str = "one_hour",
        eics: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch metering data for the given period."""
        token = await self._ensure_token()
        await self._throttle()
        params = {
            "startDateTime": start.strftime(API_DATETIME_FORMAT),
            "endDateTime": end.strftime(API_DATETIME_FORMAT),
            "resolution": resolution,
        }
        if eics:
            params["meteringPointEics"] = ",".join(eics)

        try:
            async with self._session.get(
                f"{BASE_URL}/api/public/v1/metering-data",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                await self._check_response(resp, "Metering data request failed")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise EstfeedApiError(f"Connection error: {err}") from err


class GasPriceClient:
    """Client for the Elering gas trade price API (public, no auth)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_gas_price(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch daily gas exchange prices for the given period.

        Returns list of {timestamp: int, price: float} from the common Baltic area.
        """
        params = {
            "start": start.strftime(PRICE_API_DATETIME_FORMAT),
            "end": end.strftime(PRICE_API_DATETIME_FORMAT),
        }
        try:
            async with self._session.get(GAS_PRICE_URL, params=params, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Gas price API returned %s", resp.status)
                    return []
                data = await resp.json()
                if not data.get("success"):
                    return []
                return [
                    entry
                    for entry in data.get("data", {}).get("common", [])
                    if entry.get("price") is not None
                ]
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch gas price: %s", err)
            return []


class ElectricityPriceClient:
    """Client for the Elering NPS electricity price API (public, no auth)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_electricity_prices(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch electricity prices for the given period.

        Returns list of {timestamp: int, price: float} for Estonia (EE).
        Prices are in EUR/MWh, 15-minute resolution.
        """
        params = {
            "start": start.strftime(PRICE_API_DATETIME_FORMAT),
            "end": end.strftime(PRICE_API_DATETIME_FORMAT),
        }
        try:
            async with self._session.get(
                ELECTRICITY_PRICE_URL, params=params, timeout=_REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Electricity price API returned %s", resp.status)
                    return []
                data = await resp.json()
                if not data.get("success"):
                    return []
                return [
                    entry
                    for entry in data.get("data", {}).get("ee", [])
                    if entry.get("price") is not None
                ]
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch electricity price: %s", err)
            return []


class OpenMeteoClient:
    """Client for the Open-Meteo weather API (free, no auth)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_hourly_temperatures(
        self,
        latitude: float,
        longitude: float,
        past_days: int = 7,
        forecast_days: int = 1,
    ) -> dict[datetime, float]:
        """Fetch hourly temperatures for the given location.

        Returns a dict mapping UTC datetime -> temperature in °C.
        """
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": "temperature_2m",
            "past_days": past_days,
            "forecast_days": forecast_days,
            "timeformat": "iso8601",
            "timezone": "UTC",
        }
        try:
            async with self._session.get(OPEN_METEO_URL, params=params, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Open-Meteo API returned %s", resp.status)
                    return {}
                data = await resp.json()
                times = data.get("hourly", {}).get("time", [])
                temps = data.get("hourly", {}).get("temperature_2m", [])
                result: dict[datetime, float] = {}
                for t, temp in zip(times, temps):
                    if temp is not None:
                        dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                        result[dt] = temp
                return result
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch weather data: %s", err)
            return {}
