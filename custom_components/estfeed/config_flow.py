"""Config flow for Estfeed integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EstfeedApiClient, EstfeedAuthError, EstfeedApiError
from .const import (
    CONF_APARTMENT_AREA,
    CONF_BUILDING_AREA,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    DOMAIN,
    get_area_config,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLIENT_ID): str,
        vol.Required(CONF_CLIENT_SECRET): str,
        vol.Required(CONF_APARTMENT_AREA): vol.Coerce(float),
        vol.Required(CONF_BUILDING_AREA): vol.Coerce(float),
    }
)


class EstfeedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Estfeed."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            client_id = user_input[CONF_CLIENT_ID]
            client_secret = user_input[CONF_CLIENT_SECRET]

            # Prevent duplicate entries
            await self.async_set_unique_id(client_id)
            self._abort_if_unique_id_configured()

            # Validate credentials
            session = async_get_clientsession(self.hass)
            api = EstfeedApiClient(session, client_id, client_secret)

            try:
                await api.authenticate()
            except EstfeedAuthError:
                errors["base"] = "invalid_auth"
            except EstfeedApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during setup")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="Estfeed Gas",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth when credentials become invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle reauth confirmation."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            reauth_entry = self._get_reauth_entry()
            api = EstfeedApiClient(
                session,
                reauth_entry.data[CONF_CLIENT_ID],
                user_input[CONF_CLIENT_SECRET],
            )
            try:
                await api.authenticate()
            except EstfeedAuthError:
                errors["base"] = "invalid_auth"
            except EstfeedApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_CLIENT_SECRET: user_input[CONF_CLIENT_SECRET]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CLIENT_SECRET): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EstfeedOptionsFlow:
        """Get the options flow."""
        return EstfeedOptionsFlow()


class EstfeedOptionsFlow(OptionsFlow):
    """Handle options for Estfeed."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_apartment, current_building = get_area_config(self.config_entry)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_APARTMENT_AREA, default=current_apartment): vol.Coerce(float),
                    vol.Required(CONF_BUILDING_AREA, default=current_building): vol.Coerce(float),
                }
            ),
        )
