"""Config flow for the Sunrun integration: sign in with a texted code."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    Site,
    SunrunCodeError,
    SunrunError,
    SunrunLoginError,
    Tokens,
    normalize_identifier,
    request_code,
    respond_code,
)
from .const import CONF_CODE, CONF_PHONE, CONF_RESEND, CONF_SITE_ID, CONF_TOKENS, DOMAIN


class SunrunConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._phone = ""
        self._identifier: dict[str, str] = {}
        self._session_token = ""
        self._tokens: Tokens | None = None
        self._site_id = ""
        self._sites: dict[str, Site] = {}

    async def _send_code(self, phone: str) -> str | None:
        """Text a login code; returns an error key on failure."""
        try:
            identifier = normalize_identifier(phone)
        except ValueError:
            return "invalid_phone"
        try:
            token = await request_code(async_get_clientsession(self.hass), identifier)
        except SunrunLoginError:
            return "login_refused"
        except SunrunError:
            return "cannot_connect"
        self._phone, self._identifier, self._session_token = phone.strip(), identifier, token
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await self._send_code(user_input[CONF_PHONE]):
                errors["base"] = error
            else:
                return await self.async_step_code()
        schema = vol.Schema({vol.Required(CONF_PHONE, default=self._phone): str})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_RESEND):
                if error := await self._send_code(self._phone):
                    errors["base"] = error
            else:
                try:
                    result = await respond_code(
                        async_get_clientsession(self.hass),
                        self._identifier,
                        self._session_token,
                        user_input[CONF_CODE].strip(),
                    )
                except SunrunCodeError:
                    errors["base"] = "invalid_code"
                except SunrunError:
                    errors["base"] = "cannot_connect"
                else:
                    if not result.sites:
                        return self.async_abort(reason="no_sites")
                    self._tokens = result.tokens
                    self._sites = {s.site_id: s for s in result.sites}
                    return await self._after_login()
        schema = vol.Schema(
            {vol.Required(CONF_CODE): str, vol.Optional(CONF_RESEND, default=False): bool}
        )
        return self.async_show_form(step_id="code", data_schema=schema, errors=errors)

    async def _after_login(self) -> ConfigFlowResult:
        if self.source == "reauth":
            entry = self._get_reauth_entry()
            if entry.data[CONF_SITE_ID] not in self._sites:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_TOKENS: self._tokens.as_dict(), CONF_PHONE: self._phone},
            )
        if len(self._sites) == 1:
            return await self._pick_site(next(iter(self._sites)))
        return await self.async_step_site()

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
                data={
                    CONF_PHONE: self._phone,
                    CONF_TOKENS: self._tokens.as_dict(),
                    CONF_SITE_ID: self._site_id,
                    CONF_NAME: name,
                },
            )
        default = self._sites[self._site_id].name
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=default): str}),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._phone = entry_data.get(CONF_PHONE, "")
        # With a stored number, a new code goes out straight away; otherwise ask for it.
        if self._phone and not await self._send_code(self._phone):
            return await self.async_step_code()
        return await self.async_step_user()
