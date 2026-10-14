"""DRADIS as a conversation agent.

Each sentence the pipeline did not handle locally is POSTed to the add-on's
`/api/assist`, which answers with `{"speech": "..."}` — already plain spoken
prose, so nothing is formatted here.
"""

from typing import Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_TOKEN, CONF_URL, DOMAIN, TIMEOUT

# Spoken when DRADIS cannot be reached at all.
_UNREACHABLE = {
    "it": "DRADIS non risponde in questo momento.",
    "en": "DRADIS is not answering right now.",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([DradisConversationEntity(entry)])


class DradisConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = False

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "DRADIS",
            "manufacturer": "DRADIS add-on",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        speech = await self._ask(user_input)
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id=user_input.agent_id, content=speech)
        )
        response = intent.IntentResponse(language=user_input.language)
        response.async_set_speech(speech)
        return conversation.ConversationResult(
            response=response, conversation_id=chat_log.conversation_id
        )

    async def _ask(self, user_input: conversation.ConversationInput) -> str:
        lang = (user_input.language or "en").split("-")[0]
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                f"{self.entry.data[CONF_URL].rstrip('/')}/api/assist",
                json={
                    "text": user_input.text,
                    "language": user_input.language,
                    "conversation_id": user_input.conversation_id,
                    "device_id": user_input.device_id,
                },
                headers={"Authorization": f"Bearer {self.entry.data[CONF_TOKEN]}"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("speech") or _UNREACHABLE.get(lang, _UNREACHABLE["en"])
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
        return _UNREACHABLE.get(lang, _UNREACHABLE["en"])
