"""API clients for Estfeed metering data and Elering electricity prices."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

import aiohttp

from .const import BASE_URL, ELERING_PRICE_URL, TOKEN_URL

_LOGGER = logging.getLogger(__name__)


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
        self._token: Optional[str] = None
        self._token_expiry: float = 0

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
            ) as resp:
                if resp.status in (401, 403):
                    raise EstfeedAuthError("Invalid credentials")
                if resp.status != 200:
                    text = await resp.text()
                    raise EstfeedApiError(f"Token request failed: {resp.status} {text}")
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
        params = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            async with self._session.get(
                f"{BASE_URL}/api/public/v1/metering-point-eics",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status in (401, 403):
                    raise EstfeedAuthError("Authentication failed")
                if resp.status != 200:
                    text = await resp.text()
                    raise EstfeedApiError(f"Metering points request failed: {resp.status} {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise EstfeedApiError(f"Connection error: {err}") from err

    async def get_metering_data(
        self,
        start: datetime,
        end: datetime,
        resolution: str = "one_day",
        eics: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Fetch metering data for the given period."""
        token = await self._ensure_token()
        params = {
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "resolution": resolution,
        }
        if eics:
            params["meteringPointEics"] = ",".join(eics)

        try:
            async with self._session.get(
                f"{BASE_URL}/api/public/v1/metering-data",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status in (401, 403):
                    raise EstfeedAuthError("Authentication failed")
                if resp.status != 200:
                    text = await resp.text()
                    raise EstfeedApiError(f"Metering data request failed: {resp.status} {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise EstfeedApiError(f"Connection error: {err}") from err


class EleringPriceClient:
    """Client for the public Elering NordPool electricity price API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def get_current_price(self) -> Optional[float]:
        """Get the current hour electricity spot price in EUR/MWh."""
        try:
            async with self._session.get(
                f"{ELERING_PRICE_URL}/EE/current"
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Elering price API returned %s", resp.status)
                    return None
                data = await resp.json()
                prices = data.get("data", [])
                if prices:
                    return prices[0].get("price")
                return None
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch electricity price: %s", err)
            return None

    async def get_prices(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Get historical prices for Estonia. Returns list of {timestamp, price} in EUR/MWh."""
        params = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            async with self._session.get(
                ELERING_PRICE_URL, params=params
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Elering price API returned %s", resp.status)
                    return []
                data = await resp.json()
                return data.get("data", {}).get("ee", [])
        except aiohttp.ClientError as err:
            _LOGGER.warning("Failed to fetch electricity prices: %s", err)
            return []
