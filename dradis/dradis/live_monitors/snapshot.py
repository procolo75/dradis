"""
live_monitors/snapshot.py
──────────────────────────
Shared vocabulary for the on-demand snapshots behind the `/rain` and `/storm`
Telegram commands.

Why this module exists
──────────────────────
Both ring monitors need to answer the same question — *where does this monitor
think it is, and should you believe it* — and neither of them needed to answer it
before, because a monitor that cannot trust its position simply goes silent. That
silence is correct behaviour and useless diagnostics: from the outside, "frozen
because the fix is 40 minutes old" and "nothing is happening" look identical.

`current()`, deliberately, not `usable()`
─────────────────────────────────────────
`position_manager.usable()` applies the age and accuracy thresholds and returns
None when they are not met — which is precisely the case a diagnostic command
exists to explain. Asking it would reduce the interesting answer to "no
position". `current()` returns the last fix whatever its state, and the
thresholds are then applied HERE, so the report can say *why* the monitor is not
using it.

Describing is not resolving
───────────────────────────
Nothing in this module feeds a decision. `_resolve_origin()` stays untouched in
both monitors, so the storm front's shipped and tested decision path is not
disturbed by a feature that only ever reads.
"""

import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .geo import direction_label
from .position import position_manager


@dataclass(frozen=True)
class OriginInfo:
    """Where a monitor believes it is, and whether that belief is usable."""

    lat: float | None
    lon: float | None
    following: bool                  # True when it follows a named position
    source_name: str                 # position name, or the configured location
    usable: bool
    reason: str = ""                 # why not, when `usable` is False
    age_sec: float | None = None
    accuracy_m: float | None = None
    speed_kmh: float | None = None
    course_deg: float | None = None
    moving: bool = False
    missing: bool = False            # follows a position that no longer exists

    @property
    def has_fix(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def map_url(self) -> str:
        """A link the user can tap to see the point on a real map.

        The whole question behind `/rain` and `/storm` is "is it actually where I
        am", and no amount of decimal places answers that as directly as looking.
        """
        if not self.has_fix:
            return ""
        return (f"https://www.openstreetmap.org/?mlat={self.lat:.5f}"
                f"&mlon={self.lon:.5f}#map=13/{self.lat:.5f}/{self.lon:.5f}")

    def heading_label(self, lang: str) -> str | None:
        if not self.moving or self.course_deg is None:
            return None
        return direction_label(self.course_deg, lang)


def describe_origin(monitor, now: float) -> OriginInfo:
    """Report a monitor's origin without consulting — or disturbing — its decisions.

    `monitor` is a StormFrontLiveMonitor or a RainFrontLiveMonitor; only the
    configuration attributes they share are read.
    """
    fixed = (float(monitor.latitude), float(monitor.longitude))

    if not monitor.position_id:
        return OriginInfo(
            lat=fixed[0], lon=fixed[1], following=False,
            source_name=monitor.location or monitor.name, usable=True,
        )

    name = position_manager.name_of(monitor.position_id)
    if name is None:
        # The position was deleted and the monitor still points at it. It cannot
        # alert at all, and nothing else about it is worth reporting.
        return OriginInfo(
            lat=None, lon=None, following=True, source_name=monitor.position_id,
            usable=False, missing=True,
            reason="the position it follows no longer exists",
        )

    state = position_manager.current(monitor.position_id, now)
    if state is None:
        return OriginInfo(
            lat=None, lon=None, following=True, source_name=name, usable=False,
            reason="no fix received yet",
        )

    max_age = position_manager.max_age_sec(monitor.position_id)
    source = position_manager.get(monitor.position_id)
    max_accuracy = getattr(source, "max_accuracy_m", None)

    reason = ""
    if state.age_sec > max_age:
        reason = (f"the last fix is {state.age_sec / 60:.0f} min old, past the "
                  f"{max_age / 60:.0f} min limit")
    elif (max_accuracy is not None and state.accuracy_m is not None
            and state.accuracy_m > max_accuracy):
        reason = (f"the fix is accurate to ±{state.accuracy_m:.0f} m, past the "
                  f"±{max_accuracy:.0f} m limit")

    # No distance from the configured `location` is reported, deliberately. For a
    # monitor that follows a phone those coordinates are dead configuration —
    # whatever default it was created with — so the line read "176 km from the
    # configured location" and anchored to nothing. v4.1.1 removed exactly that
    # false anchor from `/monitors`; it must not come back through a diagnostic.
    return OriginInfo(
        lat=state.lat, lon=state.lon, following=True, source_name=name,
        usable=not reason, reason=reason,
        age_sec=state.age_sec, accuracy_m=state.accuracy_m,
        speed_kmh=state.speed_kmh, course_deg=state.course_deg,
        moving=state.moving,
    )


def preview_alert(frame, edges: list[float], ring_count: int):
    """A RingAlert describing the CURRENT picture, for the chart to draw.

    The renderers accept `alert=None`, but then they omit the dominant sector and
    the front marker — the two things that make the picture read like an alert.
    Since the whole point of the command is "show me what I would receive", the
    snapshot synthesises the alert the frame would produce.

    It is thrown away immediately: it never reaches a tracker, so it cannot
    advance anything.
    """
    from .storm_front_core import RingAlert, ring_of

    dominant = frame.dominant
    if dominant is None:
        return None
    ring = ring_of(dominant.front_km, edges)
    return RingAlert(
        # Ring 0 means the front is outside the alert radius. The picture is
        # still worth drawing, so clamp to the outermost ring for display and
        # let the caption report the true distance.
        ring=max(1, ring),
        ring_count=ring_count,
        ring_edge_km=edges[max(0, ring - 1)],
        front_km=dominant.front_km,
        bearing_deg=dominant.bearing_deg,
        sector=dominant.sector,
        strikes=dominant.count,
        strikes_in_radius=frame.strikes_in_radius,
        secondary=tuple(r for r in frame.active if r is not dominant)[:2],
        is_innermost=ring >= ring_count,
    )


@dataclass(frozen=True)
class Snapshot:
    """One monitor's perception right now. Produced without touching its state."""

    monitor_id: str
    name: str
    kind: str                        # "rain" or "storm"
    language: str
    tz_name: str
    status: str                      # running / degraded / stopped
    origin: OriginInfo
    running: bool

    # Perception — all None when the monitor cannot see.
    blind_reason: str = ""
    front_km: float | None = None
    front_bearing_deg: float | None = None
    activity: int = 0                # strikes, or km² of rain
    picture: bytes | None = None

    # Event state, READ ONLY. Reported so the user can tell an open event from a
    # quiet sky, never advanced.
    event_open: bool = False
    notified_ring: int = 0
    ring_count: int = 4
    radius_km: float = 30.0

    # Rain only.
    radar_t: float | None = None
    radar_age_sec: float | None = None
    coverage: float | None = None
    peak_mmh: float | None = None
    field_speed_kmh: float | None = None
    field_bearing_deg: float | None = None
    encounter_minutes: float | None = None
    encounter_miss_km: float | None = None
    one_shot: bool = False           # radar fetched on demand, monitor not running

    # Storm only.
    feed_connected: bool = True




# ── Caption ───────────────────────────────────────────────────────────────────
#
# Formatting lives here rather than in the Telegram layer for one practical
# reason: this module imports nothing that opens a socket or needs an API token,
# so the wording is unit-testable without stubbing python-telegram-bot, the LLM
# SDKs and /data/options.json into existence.

def format_caption(snap, voice: bool = False) -> str:
    """The snapshot as a Telegram caption. `voice=True` is the Car Mode wording.

    Car Mode is not a text filter applied afterwards — half of what has to go is
    a whole LINE that only makes sense on a screen, and no amount of stripping
    icons turns "fix appena aggiornato · ±12 m · non si sta muovendo" into
    something worth hearing at the wheel. So the caller says what it needs and
    this function omits it at the source.

    What voice mode drops is everything that answers "is the instrument healthy":
    coordinates, the map link, fix age and accuracy, radar coverage, the open
    event, and the reassurance that nothing was changed. What it KEEPS is
    everything that answers "is weather coming at me", plus every line that
    explains a failure — a blind monitor, a switched-off one, a stale feed. Those
    are the reason you asked, and dropping them would leave silence sounding
    exactly like calm.

    It stays a parameter rather than a settings lookup so this module keeps
    importing nothing that opens a socket, which is what makes it testable.
    """
    it = snap.language == "it"
    icon = "🌧️" if snap.kind == "rain" else "🌩️"

    # The monitor and its data source are reported SEPARATELY, and deliberately.
    # `status()` collapses them — it returns the FEED's state whenever the poll
    # task is alive — so a perfectly active storm monitor whose Blitzortung
    # subscription has not connected yet came out as "🔴 Stopped". That reads as
    # "this thing is off" when the truth is "it is running and cannot see", which
    # are opposite instructions to the user.
    if snap.running:
        badge = "🟢 Attivo" if it else "🟢 Active"
    else:
        badge = "⏸️ Spento" if it else "⏸️ Off"

    lines = [f"{icon} <b>{html.escape(snap.name)}</b> — "
             + ("situazione ora" if it else "right now")]
    # Spoken, "Active" every single time is a word you learn to talk over, and
    # the one time it says "Off" you talk over that too. Voice mode reports the
    # status only when it is not the expected one — the same rule
    # `_format_source_health` already applies to the feed.
    if not voice or not snap.running:
        lines.append(badge)
    source = _format_source_health(snap, it)
    if source:
        lines.append(source)
    lines.append("")
    lines += _format_origin(snap.origin, it, voice)

    if snap.blind_reason:
        lines.append("")
        lines.append(("⚠️ <b>Cieco:</b> " if it else "⚠️ <b>Blind:</b> ")
                     + html.escape(snap.blind_reason))
        lines.append("Per questo non manda avvisi." if it
                     else "This is why it is not alerting.")
    else:
        lines.append("")
        lines += (_format_rain(snap, it, voice) if snap.kind == "rain"
                  else _format_storm(snap, it))

    lines.append("")
    if not snap.running and not snap.blind_reason:
        # Otherwise "🔴 Stopped" next to a live picture reads as a contradiction:
        # the image is real, but nothing will arrive on its own.
        lines.append(("⏸️ Il monitor è spento: questa è un'anteprima, non "
                      "riceverai avvisi finché non lo riattivi.") if it else
                     ("⏸️ The monitor is off: this is a preview, nothing will "
                      "arrive on its own until you enable it."))
    # Both of these describe the MONITOR's bookkeeping, not the weather. On
    # screen they are what makes the command trustworthy; read aloud they are two
    # more sentences between the driver and the thing that is coming at them.
    if not voice:
        if snap.event_open:
            lines.append((f"🎯 Evento aperto: anello {snap.notified_ring}/"
                          f"{snap.ring_count} già annunciato") if it else
                         (f"🎯 Event open: ring {snap.notified_ring}/"
                          f"{snap.ring_count} already announced"))
        else:
            lines.append("🎯 Nessun evento aperto" if it else "🎯 No event open")
        lines.append(("ℹ️ Ho solo guardato: nessun avviso inviato, niente cambiato "
                      "nel monitor.") if it else
                     ("ℹ️ Only looked: no alert sent, nothing changed in the "
                      "monitor."))
    return "\n".join(lines)


def _format_source_health(snap, it: bool) -> str:
    """The data source, reported apart from the monitor.

    Only worth a line when something is wrong: a healthy feed is the expected
    case and saying so every time would bury the cases that matter.
    """
    if not snap.running:
        return ""
    if snap.kind == "storm":
        if snap.feed_connected:
            return ""
        return ("🟠 Feed fulmini non connesso — il monitor gira ma non vede"
                if it else
                "🟠 Lightning feed not connected — running, but it cannot see")
    if snap.status == "degraded":
        return ("🟠 Radar in ritardo — il monitor gira ma il dato è vecchio"
                if it else
                "🟠 Radar behind — running, but the data is stale")
    return ""


def _format_origin(origin, it: bool, voice: bool = False) -> list[str]:
    # The warning survives voice mode; the description does not. A monitor
    # following a position that was deleted is silently watching nowhere, and
    # that has to be said however you are listening.
    if origin.missing:
        return ["⚠️ " + ("Segue una posizione che non esiste più."
                         if it else "It follows a position that no longer exists.")]

    # Everything below answers "is the instrument telling the truth" — where it
    # believes it is, to five decimals, with a map to check against, how old the
    # fix is and how tight. That is the whole point of the command on a screen,
    # and pure noise through a speaker: you cannot tap a link or read a
    # coordinate while driving, and being told your phone "is not moving" when
    # you are in a moving car is worse than useless.
    if voice:
        return []

    if origin.following:
        head = (f"📍 Origine: posizione «{html.escape(origin.source_name)}»" if it
                else f"📍 Origin: position “{html.escape(origin.source_name)}”")
    else:
        head = (f"📍 Origine: coordinate fisse — {html.escape(origin.source_name)}"
                if it else
                f"📍 Origin: fixed point — {html.escape(origin.source_name)}")
    lines = [head]
    if not origin.has_fix:
        return lines

    # The coordinates answer "what does it think", the link answers "is that
    # actually where I am" — and only the second one settles the question.
    label = "apri la mappa" if it else "open the map"
    lines.append(f"   <code>{origin.lat:.5f}, {origin.lon:.5f}</code> · "
                 f"<a href=\"{origin.map_url}\">{label}</a>")

    if origin.following:
        bits = []
        if origin.age_sec is not None:
            bits.append(f"fix {_ago(origin.age_sec, it)}")
        if origin.accuracy_m is not None:
            bits.append(f"±{origin.accuracy_m:.0f} m")
        heading = origin.heading_label("it" if it else "en")
        if heading and origin.speed_kmh:
            bits.append((f"{origin.speed_kmh:.0f} km/h verso {heading}" if it
                         else f"{origin.speed_kmh:.0f} km/h towards {heading}"))
        elif origin.speed_kmh == 0.0:
            bits.append("non si sta muovendo" if it else "not moving")
        if bits:
            lines.append("   " + " · ".join(bits))
    return lines


def _format_rain(snap, it: bool, voice: bool = False) -> list[str]:
    lines = []
    if snap.radar_t is not None:
        clock = datetime.fromtimestamp(snap.radar_t, _tz(snap.tz_name)).strftime("%H:%M")
        age = (snap.radar_age_sec or 0) / 60.0
        extra = ""
        # How much of the radar frame had usable data is a quality figure for the
        # instrument, not a fact about the weather.
        if snap.coverage is not None and not voice:
            extra = (f" · copertura {snap.coverage:.0%}" if it
                     else f" · coverage {snap.coverage:.0%}")
        fetched = (" · scaricato ora" if it else " · fetched on demand") \
            if snap.one_shot else ""
        lines.append((f"📡 Radar delle {clock} ({age:.0f} min fa){extra}{fetched}"
                      if it else
                      f"📡 Radar at {clock} ({age:.0f} min ago){extra}{fetched}"))
    if snap.front_km is None:
        lines.append("🌧️ Nessuna pioggia sopra soglia nel raggio" if it
                     else "🌧️ No rain above the threshold within range")
    else:
        heading = direction_label(snap.front_bearing_deg, "it" if it else "en")
        peak = ""
        if snap.peak_mmh is not None:
            from .radar_core import intensity_label
            label = intensity_label(snap.peak_mmh, "it" if it else "en")
            peak = (f" · max {snap.peak_mmh:.1f} mm/h ({label})" if it
                    else f" · peak {snap.peak_mmh:.1f} mm/h ({label})")
        lines.append((f"🌧️ Fronte a {snap.front_km:.0f} km a {heading}{peak}" if it
                      else f"🌧️ Front {snap.front_km:.0f} km to {heading}{peak}"))
    if snap.field_speed_kmh is None:
        lines.append("🌬️ Movimento della pioggia non misurabile" if it
                     else "🌬️ Rain movement not measurable")
    else:
        heading = direction_label(snap.field_bearing_deg, "it" if it else "en")
        lines.append((f"🌬️ Pioggia verso {heading} a {snap.field_speed_kmh:.0f} km/h"
                      if it else
                      f"🌬️ Rain towards {heading} at {snap.field_speed_kmh:.0f} km/h"))
    if snap.encounter_minutes is not None:
        lines.append((f"🧭 Incontro fra {snap.encounter_minutes:.0f} min, a "
                      f"{snap.encounter_miss_km:.0f} km da te") if it else
                     (f"🧭 Closest approach in {snap.encounter_minutes:.0f} min, "
                      f"{snap.encounter_miss_km:.0f} km away"))
    return lines


def _format_storm(snap, it: bool) -> list[str]:
    lines = []
    if snap.front_km is None:
        lines.append("⚡ Nessuna attività elettrica nel raggio" if it
                     else "⚡ No lightning activity within range")
    else:
        heading = direction_label(snap.front_bearing_deg, "it" if it else "en")
        lines.append((f"⚡ Fronte a {snap.front_km:.0f} km a {heading}" if it
                      else f"⚡ Front {snap.front_km:.0f} km to {heading}"))
    lines.append((f"🔢 {snap.activity} fulmini negli ultimi 10 min" if it
                  else f"🔢 {snap.activity} strikes in the last 10 min"))
    if not snap.feed_connected:
        lines.append("🔌 Feed Blitzortung non connesso" if it
                     else "🔌 Blitzortung feed not connected")
    return lines


def _tz(tz_name: str) -> ZoneInfo:
    """The timezone configured in DRADIS settings — never the container's.

    `time.localtime()` was used here and reported UTC, because that is the
    add-on container's clock. Every other message in the codebase goes through
    the monitor's own `_tz()`; this one has to as well, or the radar timestamp
    silently disagrees with the alerts.
    """
    try:
        return ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _ago(seconds: float, it: bool) -> str:
    """How stale a fix is, worded so a fresh one does not read as "0 s ago"."""
    if seconds < 15:
        return "appena aggiornato" if it else "just updated"
    if seconds < 90:
        return f"di {seconds:.0f} s fa" if it else f"{seconds:.0f} s old"
    return f"di {seconds / 60:.0f} min fa" if it else f"{seconds / 60:.0f} min old"


__all__ = ["OriginInfo", "Snapshot", "describe_origin", "preview_alert",
           "format_caption"]
