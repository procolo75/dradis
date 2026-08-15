"""
web/routes/positions.py
────────────────────────
CRUD for named positions, plus the two things the form needs to be usable:
entity discovery filtered to coordinates, and a connection test that tries the
values on screen rather than the last saved ones.
"""

import asyncio
from uuid import uuid4

import aiomqtt
from fastapi import APIRouter, HTTPException, Query

from live_monitors.position import probe
from web.models import PositionPayload
from web.store import (
    load_positions,
    save_positions,
    load_settings,
    _notify_positions_changed,
)

router = APIRouter()

DISCOVERY_WINDOW_SEC = 3.0
PROBE_WINDOW_SEC = 6.0


def _entry(position_id: str, payload: PositionPayload) -> dict:
    return {
        "id":              position_id,
        "name":            payload.name.strip() or "Position",
        "lat_entity":      payload.lat_entity.strip(),
        "lon_entity":      payload.lon_entity.strip(),
        "accuracy_entity": payload.accuracy_entity.strip(),
        "max_age_min":     payload.max_age_min,
        "max_accuracy_m":  payload.max_accuracy_m,
        "mqtt_prefix":     payload.mqtt_prefix.strip(),
    }


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("/api/positions")
async def list_positions():
    return load_positions()


@router.post("/api/positions")
async def create_position(payload: PositionPayload):
    positions = load_positions()
    new_position = _entry(str(uuid4()), payload)
    positions.append(new_position)
    save_positions(positions)
    _notify_positions_changed()
    return new_position


@router.put("/api/positions/{position_id}")
async def update_position(position_id: str, payload: PositionPayload):
    positions = load_positions()
    for i, p in enumerate(positions):
        if p["id"] == position_id:
            positions[i] = _entry(position_id, payload)
            save_positions(positions)
            _notify_positions_changed()
            return positions[i]
    raise HTTPException(status_code=404, detail="Position not found")


@router.delete("/api/positions/{position_id}")
async def delete_position(position_id: str):
    positions = [p for p in load_positions() if p["id"] != position_id]
    save_positions(positions)
    # Monitors pointing at it are NOT rewritten. Silently converting them back to
    # a fixed place would put them somewhere the user never asked to watch; they
    # freeze instead, and the form shows the dangling reference so the cause is
    # visible rather than mysterious.
    _notify_positions_changed()
    return {"ok": True}


# ── Discovery ─────────────────────────────────────────────────────────────────

async def _sniff_state_topics(host, port, username, password, prefix) -> set[str]:
    """Entity ids seen publishing on `{prefix}/<entity>/state` for a few seconds.

    The timeout wraps the whole listen, not the loop body. Checking the clock
    after each message only works while messages keep coming: on a quiet broker —
    or one where the prefix is simply wrong, which is exactly when a user reaches
    for Discover — the iterator blocks and the request never returns.
    """
    discovered: set[str] = set()
    kwargs = {}
    if username:
        kwargs["username"] = username
    if password:
        kwargs["password"] = password

    async def collect(client):
        async for message in client.messages:
            suffix = str(message.topic)[len(prefix):].lstrip("/")
            if suffix.endswith("/state"):
                discovered.add(suffix[: -len("/state")])

    try:
        async with aiomqtt.Client(host, port, **kwargs) as client:
            await client.subscribe(f"{prefix}/#")
            try:
                await asyncio.wait_for(collect(client), timeout=DISCOVERY_WINDOW_SEC)
            except asyncio.TimeoutError:
                pass                      # the window closing is the normal exit
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MQTT discovery failed: {e}")
    return discovered


# Entity names that plausibly carry each component. Deliberately name-based:
# a value-based guess cannot tell a latitude from a longitude (both are plain
# numbers in overlapping ranges), and a wrong auto-pick is worse than no pick.
_POSITION_HINTS = {
    "latitude":  ("latitude", "_lat", "/lat", "latitudine"),
    "longitude": ("longitude", "_lon", "/lon", "_lng", "longitudine"),
    "accuracy":  ("gps_accuracy", "accuracy", "precisione", "accuratezza"),
}


def _classify_position_entity(entity_id: str) -> str | None:
    name = entity_id.lower()
    # Longitude first: "longitude" contains no "latitude", but several naming
    # schemes make "lat" a substring of unrelated words, so the more specific
    # match must win.
    for kind in ("longitude", "accuracy", "latitude"):
        if any(hint in name for hint in _POSITION_HINTS[kind]):
            return kind
    return None


@router.post("/api/positions/discover")
async def discover_position_entities(prefix: str = Query(default="")):
    """Entities that look like coordinates, split by which field they belong to.

    The generic HA discovery returns every entity on the broker, which for this
    form is a list of hundreds to hunt through for three names. Here the sniffing
    is the same; the classification is what makes the result usable. `all` is
    returned too, so an unconventional naming scheme is inconvenient rather than
    a dead end.
    """
    settings = load_settings()
    prefix = (prefix.strip().rstrip("/") if prefix.strip()
              else settings.get("mqtt_statestream_prefix", "homeassistant").rstrip("/"))

    discovered = await _sniff_state_topics(
        settings.get("mqtt_host", "core-mosquitto"),
        int(settings.get("mqtt_port", 1883)),
        settings.get("mqtt_username") or None,
        settings.get("mqtt_password") or None,
        prefix,
    )

    result: dict[str, list[str]] = {"latitude": [], "longitude": [], "accuracy": []}
    for entity in sorted(discovered):
        kind = _classify_position_entity(entity)
        if kind:
            result[kind].append(entity)
    result["all"] = sorted(discovered)
    return result


# ── Test ──────────────────────────────────────────────────────────────────────

@router.post("/api/positions/test")
async def test_position(payload: PositionPayload):
    """Test the values ON THE SCREEN, saved or not.

    Testing a form you have not saved yet is the normal case, and refusing until
    it is stored turns a diagnostic into a riddle. The running manager is left
    alone — this connects with its own throwaway client.

    The result deliberately reports an unusable fix rather than an error: "no
    position at all" and "a position from two hours ago" are different problems
    with different fixes, and collapsing them would send the user hunting through
    the wrong configuration.
    """
    if not (payload.lat_entity.strip() and payload.lon_entity.strip()):
        return {"ok": False, "status": "incomplete",
                "detail": "Set both the latitude and the longitude entity first."}

    settings = load_settings()
    manager = probe(settings, _entry("probe", payload))
    manager.start()
    try:
        # Statestream publishes retained, so a working setup answers on connect.
        # Waiting longer than this only delays telling the user it does not.
        deadline = asyncio.get_event_loop().time() + PROBE_WINDOW_SEC
        state = None
        while state is None and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
            state = manager.current("probe")

        source = manager.get("probe")
        if state is None:
            return {
                "ok": False, "status": manager.status(),
                "detail": (f"Connected to the broker, but nothing arrived on "
                           f"{source.prefix}/{payload.lat_entity.strip()}/state. "
                           f"Check the entity names and that mqtt_statestream "
                           f"includes them."),
                "stats": manager.stats(),
            }

        too_old = state.age_sec > source.max_age_sec
        too_vague = (state.accuracy_m is not None
                     and state.accuracy_m > source.max_accuracy_m)

        # Speed needs a minute of history, which a six-second probe cannot have.
        # The running manager may know, if this position is already saved.
        motion = next(
            (s for s in (_running_state(pid) for pid in _matching_ids(payload))
             if s is not None), None)

        return {
            "ok": not (too_old or too_vague),
            "status": manager.status(),
            "latitude": round(state.lat, 5),
            "longitude": round(state.lon, 5),
            "age_sec": round(state.age_sec, 1),
            "accuracy_m": state.accuracy_m,
            "speed_kmh": (None if motion is None or motion.speed_kmh is None
                          else round(motion.speed_kmh, 1)),
            "course_deg": (None if motion is None or motion.course_deg is None
                           else round(motion.course_deg)),
            "moving": bool(motion is not None and motion.moving),
            "too_old": too_old,
            "too_vague": too_vague,
            "stats": manager.stats(),
        }
    finally:
        await manager.aclose()


def _matching_ids(payload: PositionPayload) -> list[str]:
    """Saved positions reading the same latitude entity as the form."""
    return [p["id"] for p in load_positions()
            if (p.get("lat_entity") or "").strip() == payload.lat_entity.strip()]


def _running_state(position_id: str):
    from live_monitors.position import position_manager
    return position_manager.current(position_id)
