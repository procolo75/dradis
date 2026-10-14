"""DRADIS — the DRADIS add-on as a conversation agent for Assist.

The voice pipeline handles home commands locally ("Prefer handling commands
locally"); everything else is sent here and answered by DRADIS exactly as its
Telegram chat would answer it — same tools, same conversation history.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.CONVERSATION]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
