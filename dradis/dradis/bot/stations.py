"""
bot/stations.py
────────────────
The `/stations` readout: what the ground stations around a point are measuring
right now, as a Telegram message.

Formatting lives here rather than in `handlers.py` for the reason stated at the
top of `live_monitors/snapshot.py`: this module imports nothing that opens a
socket or needs an API token, so the wording is unit-testable without stubbing
python-telegram-bot into existence.

Composed by QUANTITY, not by station
────────────────────────────────────
The obvious readout — the N nearest stations, each with what it has — produces
almost nothing, because most stations are rain gauges and nothing else. Within
25 km of Bacoli, 11 of 11 official stations report precipitation and exactly 2
report wind. The eight nearest to Rome, printed as they are:

    0.5 km  Collegio Romano         22.8°C · rain 0.0mm/3h
    1.7 km  Cassiodoro              level +4.81m · rain 0.0mm/3h
    2.2 km  Tevere a Porta Portese  level -0.61m · rain 0.0mm/3h
    2.3 km  Ostiense                rain 0.0mm/3h
    2.4 km  Eleniano                rain 0.0mm/3h
    2.8 km  Rosolino Pilo           rain 0.0mm/3h
    2.9 km  Roma Macao              22.1°C · rain 0.0mm/3h
    3.6 km  Roma Flaminio           21.9°C · rain 0.0mm/3h

Five of eight lines say only "0.0 mm", and wind, humidity and pressure do not
appear at all — their stations are further out and never make the top eight.

So each line takes the nearest station that MEASURES that quantity, and carries
its own station, its own distance and its own age. It still reads several
stations; it just picks each one for what it knows rather than for where it is.

Rain is the exception, and is summarised over the whole set
───────────────────────────────────────────────────────────
It is the one quantity with dense coverage, and one dry gauge 500 m away says
nothing about a shower at 3 km — the same reason the reading has no vote in
`rain_front`. So: "2 wet out of 44", never "it is not raining".

No coordinates, no links, no numeric ids
────────────────────────────────────────
Car Mode prints this readout whole, and `car_mode.to_spoken` strips coordinate
pairs, URLs and `#\\d{3,}` ids — leaving stumps where they were. The constraint
is met at the source instead: the header is a plain place name, and the origin
block from `snapshot.py` (which carries an OpenStreetMap link) is deliberately
not reused. A test asserts the output holds none of the three.
"""

import html
from datetime import datetime

from live_monitors.gauges_core import (
    VAR_GUST, VAR_HUMIDITY, VAR_PRECIP, VAR_PRESSURE, VAR_RIVER,
    VAR_TEMP, VAR_WIND, VAR_WIND_DIR,
)
from live_monitors.geo import direction_label

# The order of the message: what the sky is doing, then what the water is doing.
# Snow depth is absent on purpose — see the note beside `VAR_SNOW` in
# `gauges_core`: it reported 113 cm in central Turin in September.
_ROWS = (VAR_TEMP, VAR_HUMIDITY, VAR_PRESSURE, VAR_WIND, VAR_GUST, VAR_RIVER)


def _ago(minutes: float, it: bool) -> str:
    if minutes < 1:
        return "ora" if it else "just now"
    if minutes < 90:
        return f"{minutes:.0f} min fa" if it else f"{minutes:.0f} min ago"
    return f"{minutes / 60:.1f} h fa" if it else f"{minutes / 60:.1f} h ago"


def _where(reading, it: bool) -> str:
    side = direction_label(reading.bearing_deg, "it" if it else "en")
    name = html.escape(reading.name)
    if reading.distance_km < 1:
        return f"{name}, qui" if it else f"{name}, here"
    return (f"{name}, {reading.distance_km:.0f} km a {side}" if it
            else f"{name}, {reading.distance_km:.0f} km to the {side}")


def _value(var: str, m, reading, it: bool) -> str | None:
    """The quantity as a phrase, or None when this build does not print it."""
    v = m.value
    if var == VAR_TEMP:
        return f"🌡️ {v - 273.15:.1f} °C"
    if var == VAR_HUMIDITY:
        return (f"💧 umidità {v:.0f}%" if it else f"💧 humidity {v:.0f}%")
    if var == VAR_PRESSURE:
        return f"🔻 {v / 100:.0f} hPa"
    if var == VAR_WIND:
        speed = f"{v * 3.6:.0f} km/h"
        gust_dir = reading.measurements.get(VAR_WIND_DIR)
        if gust_dir is not None:
            side = direction_label(gust_dir.value, "it" if it else "en")
            return (f"🌬️ vento {speed} da {side}" if it
                    else f"🌬️ wind {speed} from {side}")
        return f"🌬️ vento {speed}" if it else f"🌬️ wind {speed}"
    if var == VAR_GUST:
        return (f"💨 raffica max {v * 3.6:.0f} km/h" if it
                else f"💨 peak gust {v * 3.6:.0f} km/h")
    if var == VAR_RIVER:
        return (f"🌊 livello fiume {v:+.2f} m" if it
                else f"🌊 river level {v:+.2f} m")
    return None


def _when(var: str, m, now: float, tz, it: bool) -> str:
    """How old the reading is — and, for a maximum, what it is a maximum OVER.

    A gust carries `2,0,3600`: a peak over the past hour, not a value taken at
    the timestamp it holds. Printing it like an instantaneous reading is wrong
    by up to an hour, so the span is named instead of the age.
    """
    clock = datetime.fromtimestamp(m.observed_at, tz).strftime("%H:%M")
    if var == VAR_GUST and m.window_min >= 30:
        return (f"nell'ora fino alle {clock}" if it
                else f"in the hour to {clock}")
    return _ago(max(0.0, (now - m.observed_at) / 60.0), it)


# Below this a station is not "wet", it is a gauge that twitched. The rate is
# printed to one decimal, so anything under 0.1 renders as "0.0 mm/h" — and
# "3 gauges wet, peak 0.0 mm/h" is a sentence that contradicts itself in its own
# second half. The bar is the printed resolution, so the count and the number
# beside it can never disagree.
_WET_MMH = 0.1


def _rain_line(gauges, now: float, it: bool) -> str:
    """The whole set of rain gauges in one sentence.

    Never "it is not raining": an absence is stated as an absence. What the
    reader is told is how many instruments are there and what they caught.
    """
    total = [r for r in gauges if VAR_PRECIP in r.measurements]
    if not total:
        return ""
    wet = sorted((r for r in total if (r.mmh or 0) >= _WET_MMH),
                 key=lambda r: -(r.mmh or 0))
    if not wet:
        return (f"☀️ nessuna pioggia su {len(total)} pluviometri" if it
                else f"☀️ no rain on any of {len(total)} gauges")
    top = wet[0]
    head = (f"☔ pioggia: {len(wet)} pluviometri bagnati su {len(total)}"
            if it else
            f"☔ rain: {len(wet)} of {len(total)} gauges wet")
    return (f"{head} — max {top.mmh:.1f} mm/h a {_where(top, it)}" if it
            else f"{head} — peak {top.mmh:.1f} mm/h at {_where(top, it)}")


def format_stations(view, place: str, *, lang: str = "it", tz=None,
                    radius_km: float, now: float) -> str:
    """The readout, or the reason there isn't one.

    `view` follows `gauges.observe`'s three-way contract and this is where the
    three become three different sentences: None is a question that went
    unanswered, an empty view is an answer with nothing recent in range, and a
    view with readings is a reading.
    """
    it = lang == "it"
    where = html.escape(place)
    head = (f"📍 <b>Stazioni al suolo — {where}</b>" if it
            else f"📍 <b>Ground stations — {where}</b>")

    if view is None:
        return head + ("\n🔌 Rete di stazioni non raggiungibile" if it
                       else "\n🔌 Station network unreachable")
    if not view.readings:
        return head + (
            f"\n🔎 Nessuna lettura recente entro {radius_km:.0f} km" if it
            else f"\n🔎 No recent reading within {radius_km:.0f} km")

    lines = [head]
    for var in _ROWS:
        best = next((r for r in sorted(view.readings,
                                       key=lambda x: x.distance_km)
                     if var in r.measurements), None)
        if best is None:
            continue                       # no station has it: no line, not a blank
        m = best.measurements[var]
        text = _value(var, m, best, it)
        if text:
            lines.append(f"{text} · {_where(best, it)} · "
                         f"{_when(var, m, now, tz, it)}")

    rain = _rain_line(view.readings, now, it)
    if rain:
        lines.append(rain)

    nets = sorted({r.network for r in view.readings})
    tail = (f"🔎 {len(view.readings)} stazioni entro {radius_km:.0f} km" if it
            else f"🔎 {len(view.readings)} stations within {radius_km:.0f} km")
    if len(nets) <= 3:
        tail += " · " + html.escape(", ".join(nets))
    lines.append(tail)
    return "\n".join(lines)


__all__ = ["format_stations"]
