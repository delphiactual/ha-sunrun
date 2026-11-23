"""The Sunrun integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SunrunClient
from .const import CONF_TOKEN
from .coordinator import SunrunCoordinator

PLATFORMS = [Platform.SENSOR]

type SunrunConfigEntry = ConfigEntry[SunrunCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SunrunConfigEntry) -> bool:
    client = SunrunClient(async_get_clientsession(hass), entry.data[CONF_TOKEN])
    coordinator = SunrunCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SunrunConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
