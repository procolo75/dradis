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
    # Tools to attach for this task: None or ["*"] = all available tools, or an
    # explicit list of tool names / capability ids (e.g. ["get_unread_emails",
    # "create_calendar_event"]). Fewer tools = smaller prompt.
    tools:           list[str] | None = None


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
    min_level:       int   = 2
    alert_mode:      str   = "direct"
    instructions:    str   = ""
    cape_sat:        float     = 1200.0
    li_sat:          float     = 5.0
    cin_supp:        float     = 100.0
    weather_models:  list[str] = []
    chart_variables: list[str] = []
    telegram_bot_id: str       = "default"


class LiveMonitorPayload(BaseModel):
    name:            str
    enabled:         bool      = False
    type:            str       = "storm_front"
    location:        str       = ""
    latitude:        float     = 0.0
    longitude:       float     = 0.0
    radius_km:       float     = 30.0
    language:        str       = "it"
    areas:           list[str] = []
    quiet_start:     str       = ""
    quiet_end:       str       = ""
    # Football: which of the two minute windows are enabled. The ids are stable;
    # the minutes below are what moves. Monitors saved before the minutes became
    # settable hold the old bounds-as-id labels ("55-65") and are read back by
    # live_monitors/football.py._window_specs.
    windows:         list[str] = ["early", "late"]
    window_early_start:    int   = 55
    window_early_end:      int   = 65
    window_early_max_odds: float = 2.0
    window_late_start:     int   = 75
    window_late_end:       int   = 81
    # Each window has its own cap on the trailing side's next-goal odds. 0 means
    # no cap — the late window's default, because that is what it did before the
    # cap became per-window, and a stricter default would silently drop alerts
    # from every monitor already saved.
    window_late_max_odds:  float = 0.0
    # Deprecated: the single cap, which gated the early window only. Kept so a
    # monitor saved before this release keeps its value until it is saved again.
    max_odds:        float     = 2.0
    telegram_bot_id: str       = "default"
    # Storm front and rain front — how many approach updates a single event may
    # produce. 2, 3 or 4; the ring distances are derived proportionally from
    # radius_km.
    ring_count:      int       = 4
    # Storm front and rain front — attach the radar picture to each ring message.
    chart:           bool      = True
    # Rain front only — the intensity, in mm/h, below which rain is not worth a
    # message. A discharge is a discharge, but rain is a continuum, so whether
    # drizzle deserves a notification is a preference rather than a constant.
    min_mmh:         float     = 1.0
    # Rain front only — also fetch the probability-of-hail product and mention it
    # when the front carries a real chance of hail.
    hail:            bool      = False
    # Storm front and rain front — empty centres the radar on latitude/longitude
    # (the historical behaviour, and the default). Otherwise it is the id of a
    # named position, and the monitor follows THAT and nothing else: there is no
    # fallback, because watching your house instead of you is not a gentle
    # degradation, it is answering a different question without saying so.
    position_id:     str       = ""


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
    # Tools DRADIS may use when reacting to a state change in LLM mode:
    # [] = no tools (default — smallest prompt), ["*"] = all available, or an
    # explicit list of tool names / capability ids (e.g. ["create_task"]).
    tools:           list[str] = []


class BotPayload(BaseModel):
    name:    str
    token:   str
    chat_id: int


class SettingsPayload(BaseModel):
    provider:             str  = "openrouter"
    agent_instructions:   str  = "You are DRADIS, a versatile AI assistant."
    model:                str  = "nvidia/nemotron-3-nano-30b-a3b:free"
    max_tokens:           int  = 2048
    temperature:          float = 0.2
    tool_call_limit:      int  = 3
    tpm_limit:            int  = 0
    token_usage_enabled:  bool = False
    tools_usage_enabled:  bool = False
    tool_errors_enabled:  bool = True
    history_enabled:      bool = True
    history_depth:        int  = 2
    startup_message:      str  = "✅ DRADIS online and ready."
    timezone:             str  = "UTC"
    car_mode_enabled:     bool = False
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


class PositionPayload(BaseModel):
    """One named position. Thresholds are per position, not global: two phones
    have different GPS chips and different reporting habits."""
    name:            str   = "Position"
    lat_entity:      str   = ""
    lon_entity:      str   = ""
    accuracy_entity: str   = ""
    max_age_min:     float = 15.0
    max_accuracy_m:  float = 500.0
    mqtt_prefix:     str   = ""


class SpeedtestPayload(BaseModel):
    models: list[str]
