"""Config flow for DRADIS.

Normally there is nothing to type: the add-on announces itself through the
Supervisor (`discovery: [dradis]`) with its URL and token, and the user only
confirms. The manual step exists for when discovery is not available.
"""

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import CONF_TOKEN, CONF_URL, DOMAIN


async def _check(hass, url: str, token: str) -> str | None:
    """None when DRADIS accepts the token, else an error key."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(f"{url.rstrip('/')}/api/assist/status",
                               headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            return None if resp.status == 200 else "cannot_connect"
    except (aiohttp.ClientError, TimeoutError):
        return "cannot_connect"


class DradisConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        # A new port or hostname (add-on reinstalled) updates the existing entry.
        self._abort_if_unique_id_configured(updates={
            CONF_URL: discovery_info.config[CONF_URL],
            CONF_TOKEN: discovery_info.config[CONF_TOKEN],
        })
        self._discovered = {CONF_URL: discovery_info.config[CONF_URL],
                            CONF_TOKEN: discovery_info.config[CONF_TOKEN]}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="DRADIS", data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm",
                                    description_placeholders={"url": self._discovered[CONF_URL]})

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await _check(self.hass, user_input[CONF_URL], user_input[CONF_TOKEN])
            if error is None:
                return self.async_create_entry(title="DRADIS", data=user_input)
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_URL): str,
                                    vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )
