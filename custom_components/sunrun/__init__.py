"""The Sunrun integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SunrunClient, Tokens
from .const import CONF_TOKENS
from .coordinator import SunrunCoordinator

PLATFORMS = [Platform.SENSOR]

type SunrunConfigEntry = ConfigEntry[SunrunCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SunrunConfigEntry) -> bool:
    def save_tokens(tokens: Tokens) -> None:
        # A refresh token that rotates must be stored or the next refresh will fail.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKENS: tokens.as_dict()}
        )

    client = SunrunClient(
        async_get_clientsession(hass), Tokens.from_dict(entry.data[CONF_TOKENS]), save_tokens
    )
    coordinator = SunrunCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunrunConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: SunrunConfigEntry) -> bool:
    if entry.version == 1:
        # v1 stored one pasted token. It keeps working until it expires, then reauth asks
        # for a mobile number and signs in properly.
        data = {k: v for k, v in entry.data.items() if k != "token"}
        data[CONF_TOKENS] = Tokens(entry.data["token"]).as_dict()
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True
