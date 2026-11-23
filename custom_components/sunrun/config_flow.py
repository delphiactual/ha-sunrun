"""Config flow for the Sunrun integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Site, SunrunAuthError, SunrunClient, SunrunError
from .const import CONF_SITE_ID, CONF_TOKEN, DOMAIN

TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


class SunrunConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._token = ""
        self._site_id = ""
        self._sites: dict[str, Site] = {}

    async def _load_sites(self, token: str) -> str | None:
        """Validate the token and cache its sites; return an error key on failure."""
        try:
            sites = await SunrunClient(async_get_clientsession(self.hass), token).get_sites()
        except SunrunAuthError:
            return "invalid_auth"
        except SunrunError:
            return "cannot_connect"
        if not sites:
            return "no_sites"
        self._token = token
        self._sites = {s.site_id: s for s in sites}
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await self._load_sites(user_input[CONF_TOKEN].strip()):
                errors["base"] = error
            elif len(self._sites) == 1:
                return await self._pick_site(next(iter(self._sites)))
            else:
                return await self.async_step_site()
        return self.async_show_form(step_id="user", data_schema=TOKEN_SCHEMA, errors=errors)

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._pick_site(user_input[CONF_SITE_ID])
        options = {sid: site.name for sid, site in self._sites.items()}
        return self.async_show_form(
            step_id="site", data_schema=vol.Schema({vol.Required(CONF_SITE_ID): vol.In(options)})
        )

    async def _pick_site(self, site_id: str) -> ConfigFlowResult:
        await self.async_set_unique_id(site_id)
        self._abort_if_unique_id_configured()
        self._site_id = site_id
        return await self.async_step_name()

    async def async_step_name(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            return self.async_create_entry(
                title=name,
                data={CONF_TOKEN: self._token, CONF_SITE_ID: self._site_id, CONF_NAME: name},
            )
        default = self._sites[self._site_id].name
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=default): str}),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if error := await self._load_sites(token):
                errors["base"] = error
            elif entry.data[CONF_SITE_ID] not in self._sites:
                errors["base"] = "wrong_account"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )
