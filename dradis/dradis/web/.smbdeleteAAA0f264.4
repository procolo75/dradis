"""
web/models.py
─────────────
Pydantic request/response models for the DRADIS web API.
"""

from pydantic import BaseModel


class AgentPayload(BaseModel):
    provider:     str
    model:        str
    instructions: str
    active:       bool = True


class TaskPayload(BaseModel):
    name:            str
    enabled:         bool = False
    cron:            str  = "0 8 * * *"
    instructions:    str  = ""
    telegram_bot_id: str  = "default"


class MonitorPayload(BaseModel):
    name:            str
    enabled:         bool  = False
    cron:            str   = "0 7 * * *"
    type:            str   = "thunderstorm"
    location:        str   = ""
    days:            int   = 2
    language:        str   = "it"
    hours_ahead:     int   = 2
    seismic_area:    str   = "flegrei"
    time_range:      str   = "last_24h"
    alert_mode:      str   = "direct"
    instructions:    str   = ""
    cape_sat:        float = 1200.0
    li_sat:          float = 5.0
    cin_supp:        float = 100.0
    telegram_bot_id: str   = "default"


class LiveMonitorPayload(BaseModel):
    name:            str
    enabled:         bool      = False
    type:            str       = "lightning"
    location:        str       = ""
    latitude:        float     = 0.0
    longitude:       float     = 0.0
    radius_km:       float     = 100.0
    language:        str       = "it"
    areas:           list[str] = []
    quiet_start:     str       = ""
    quiet_end:       str       = ""
    windows:         list[str] = ["55-65", "75-81"]
    telegram_bot_id: str       = "default"


class HaMonitorPayload(BaseModel):
    name:            str
    enabled:         bool  = False
    entities:        list  = []
    instructions:    str   = ""
    cooldown_min:    float = 60.0
    language:        str   = "it"
    filter_states:   list  = []
    alert_mode:      str   = "llm"
    direct_template: str   = ""
    mqtt_prefix:     str   = ""
    telegram_bot_id: str   = "default"


class BotPayload(BaseModel):
    name:    str
    token:   str
    chat_id: int


class SettingsPayload(BaseModel):
    provider:             str  = "openrouter"
    agent_instructions:   str  = "You are DRADIS, a versatile AI assistant."
    model:                str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    history_enabled:      bool = True
    history_depth:        int  = 2
    startup_message:      str  = "✅ DRADIS online and ready."
    timezone:             str  = "UTC"
    ws_enabled:           bool = False
    ws_provider:          str  = "openrouter"
    ws_model:             str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    ws_instructions:      str  = ""
    read_url_enabled:     bool = False
    weather_enabled:      bool = False
    weather_provider:     str  = "openrouter"
    weather_model:        str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    weather_instructions: str  = ""
    voice_enabled:            bool = False
    voice_provider:           str  = "groq"
    voice_model:              str  = "whisper-large-v3-turbo"
    voice_language:           str  = "it"
    voice_send_transcription: bool = True
    gcal_enabled:             bool = False
    gcal_provider:            str  = "openrouter"
    gcal_model:               str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    gcal_instructions:        str  = ""
    gmail_enabled:            bool = False
    gmail_provider:           str  = "openrouter"
    gmail_model:              str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    gmail_instructions:       str  = ""
    gtasks_enabled:           bool = False
    gtasks_provider:          str  = "openrouter"
    gtasks_model:             str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    gtasks_instructions:      str  = ""
    fallback_provider:             str = ""
    fallback_model:                str = ""
    ws_fallback_provider:          str = ""
    ws_fallback_model:             str = ""
    weather_fallback_provider:     str = ""
    weather_fallback_model:        str = ""
    gcal_fallback_provider:        str = ""
    gcal_fallback_model:           str = ""
    gmail_fallback_provider:       str = ""
    gmail_fallback_model:          str = ""
    gtasks_fallback_provider:      str = ""
    gtasks_fallback_model:         str = ""
    mqtt_host:               str  = "core-mosquitto"
    mqtt_port:               int  = 1883
    mqtt_username:           str  = ""
    mqtt_password:           str  = ""
    mqtt_statestream_prefix: str  = "homeassistant"


class SpeedtestPayload(BaseModel):
    models: list[str]
