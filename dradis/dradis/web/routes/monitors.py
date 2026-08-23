"""
web/routes/monitors.py
───────────────────────
Routes: scheduled monitors, live monitors, HA monitors, geocode, HA discovery.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import aiomqtt
from fastapi import APIRouter, HTTPException, Query

from geocode import search as geocode_search
import web.store as _store
from web.store import (
    _validate_cron_expr,
    _notify_monitors_changed,
    _notify_live_monitors_changed,
    _notify_ha_monitors_changed,
    load_monitors,
    save_monitors,
    load_live_monitors,
    save_live_monitors,
    load_ha_monitors,
    save_ha_monitors,
    load_settings,
)
from web.models import MonitorPayload, LiveMonitorPayload, HaMonitorPayload
# Shared with the position form, which needs the same sniffing with a filter on
# top. Defined there because that is where the bounded-listen fix belongs.
from web.routes.positions import _sniff_state_topics

router = APIRouter()


# ── Scheduled Monitors ────────────────────────────────────────────────────────

@router.get("/api/monitors")
async def list_monitors():
    return load_monitors()


_TYPES_WITHOUT_LOCATION = {"seismic", "backup"}


@router.post("/api/monitors")
async def create_monitor(payload: MonitorPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    if payload.type not in _TYPES_WITHOUT_LOCATION and not payload.location.strip():
        raise HTTPException(status_code=400, detail="Monitor location is required")
    monitors = load_monitors()
    monitor = {
        "id":         str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload.model_dump(),
    }
    monitors.append(monitor)
    save_monitors(monitors)
    _notify_monitors_changed()
    return monitor


@router.put("/api/monitors/{monitor_id}")
async def update_monitor(monitor_id: str, payload: MonitorPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    if payload.type not in _TYPES_WITHOUT_LOCATION and not payload.location.strip():
        raise HTTPException(status_code=400, detail="Monitor location is required")
    monitors = load_monitors()
    for i, m in enumerate(monitors):
        if m["id"] == monitor_id:
            monitors[i] = {**m, **payload.model_dump()}
            save_monitors(monitors)
            _notify_monitors_changed()
            return monitors[i]
    raise HTTPException(status_code=404, detail="Monitor not found")


@router.delete("/api/monitors/{monitor_id}")
async def delete_monitor(monitor_id: str):
    monitors = load_monitors()
    monitors = [m for m in monitors if m["id"] != monitor_id]
    save_monitors(monitors)
    _notify_monitors_changed()
    return {"ok": True}


@router.post("/api/monitors/{monitor_id}/run")
async def run_monitor_now(monitor_id: str):
    if not _store._run_monitor_fn:
        raise HTTPException(status_code=503, detail="Monitor runner not available")
    monitor = next((m for m in load_monitors() if m["id"] == monitor_id), None)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if monitor.get("type") != "seismic" and not monitor.get("location", "").strip():
        raise HTTPException(status_code=400, detail="Monitor has no location configured")
    asyncio.create_task(_store._run_monitor_fn(monitor))
    return {"ok": True}


@router.get("/api/monitors/geocode")
async def geocode_location(q: str = ""):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Query is required")
    try:
        r = await geocode_search(q, timeout=8)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Location not found: {q!r}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "name":         r.get("name", q),
        "country":      r.get("country", ""),
        "country_code": r.get("country_code", ""),
        "latitude":     r["latitude"],
        "longitude":    r["longitude"],
    }


# ── Live Monitors ─────────────────────────────────────────────────────────────

@router.get("/api/live-monitors")
async def list_live_monitors():
    return load_live_monitors()


_LIVE_TYPES_WITHOUT_LOCATION = {"football_betting"}


@router.post("/api/live-monitors")
async def create_live_monitor(payload: LiveMonitorPayload):
    if payload.type not in _LIVE_TYPES_WITHOUT_LOCATION and not payload.location.strip():
        raise HTTPException(status_code=400, detail="Location is required")
    items = load_live_monitors()
    item = {
        "id":         str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload.model_dump(),
    }
    items.append(item)
    save_live_monitors(items)
    _notify_live_monitors_changed()
    return item


@router.put("/api/live-monitors/{item_id}")
async def update_live_monitor(item_id: str, payload: LiveMonitorPayload):
    if payload.type not in _LIVE_TYPES_WITHOUT_LOCATION and not payload.location.strip():
        raise HTTPException(status_code=400, detail="Location is required")
    items = load_live_monitors()
    for i, m in enumerate(items):
        if m["id"] == item_id:
            items[i] = {**m, **payload.model_dump()}
            save_live_monitors(items)
            _notify_live_monitors_changed()
            return items[i]
    raise HTTPException(status_code=404, detail="Live monitor not found")


@router.delete("/api/live-monitors/{item_id}")
async def delete_live_monitor(item_id: str):
    items = [m for m in load_live_monitors() if m["id"] != item_id]
    save_live_monitors(items)
    _notify_live_monitors_changed()
    return {"ok": True}


@router.get("/api/live-monitors/{item_id}/status")
async def get_live_monitor_status(item_id: str):
    if _store._get_live_monitor_status_fn:
        return {"status": _store._get_live_monitor_status_fn(item_id)}
    return {"status": "unknown"}


# ── HA Monitors ───────────────────────────────────────────────────────────────

@router.get("/api/ha-monitors")
async def list_ha_monitors():
    return load_ha_monitors()


@router.post("/api/ha-monitors")
async def create_ha_monitor(payload: HaMonitorPayload):
    items = load_ha_monitors()
    item = {
        "id":         str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload.model_dump(),
    }
    items.append(item)
    save_ha_monitors(items)
    _notify_ha_monitors_changed()
    return item


@router.put("/api/ha-monitors/{item_id}")
async def update_ha_monitor(item_id: str, payload: HaMonitorPayload):
    items = load_ha_monitors()
    for i, m in enumerate(items):
        if m["id"] == item_id:
            items[i] = {**m, **payload.model_dump()}
            save_ha_monitors(items)
            _notify_ha_monitors_changed()
            return items[i]
    raise HTTPException(status_code=404, detail="HA monitor not found")


@router.delete("/api/ha-monitors/{item_id}")
async def delete_ha_monitor(item_id: str):
    items = [m for m in load_ha_monitors() if m["id"] != item_id]
    save_ha_monitors(items)
    _notify_ha_monitors_changed()
    return {"ok": True}


@router.get("/api/ha-monitors/{item_id}/status")
async def get_ha_monitor_status(item_id: str):
    if _store._get_ha_monitor_status_fn:
        return {"status": _store._get_ha_monitor_status_fn(item_id)}
    return {"status": "unknown"}


@router.post("/api/ha/test")
async def test_ha_connection():
    settings = load_settings()
    host     = settings.get("mqtt_host", "core-mosquitto")
    port     = int(settings.get("mqtt_port", 1883))
    username = settings.get("mqtt_username") or None
    password = settings.get("mqtt_password") or None
    kwargs   = {}
    if username:
        kwargs["username"] = username
    if password:
        kwargs["password"] = password
    try:
        async with aiomqtt.Client(host, port, **kwargs):
            pass
        return {"ok": True, "host": host, "port": port}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/radar/test")
async def test_radar(latitude: float | None = Query(default=None),
                     longitude: float | None = Query(default=None),
                     radius_km: float = Query(default=30.0)):
    """Can we reach the radar, and does it actually cover this point?

    Always answers 200 with a verdict rather than raising, as the positions probe
    does: "the service is up but your house is in a blind spot" is a useful
    answer, not an error, and it is the one thing a user cannot discover from the
    outside. Coverage is the load-bearing number here — a monitor watching a disc
    the network cannot see into would report permanent calm.
    """
    from live_monitors.radar import PRODUCT_RAIN, fetch_latest
    from live_monitors.radar_core import (
        coverage_fraction, peak_in_disc, sample)
    from live_monitors.storm_front_core import OBSERVE_FACTOR, clamp_radius

    try:
        grid, lag_sec = await fetch_latest(PRODUCT_RAIN)
    except Exception as e:
        return {"ok": False, "status": "unreachable", "detail": str(e)}

    result = {
        "ok": True,
        "status": "ok",
        "product": grid.product,
        "measured_at": grid.t,
        "lag_sec": round(lag_sec),
        "grid": f"{grid.gt.cols}x{grid.gt.rows} @ {grid.gt.pixel_m / 1000:.0f} km",
    }
    # No point asked about: the reachability half of the answer is all there is.
    # Not a 0.0 sentinel — null island is a real coordinate, and a monitor pinned
    # there by a bad config deserves the same honest verdict as any other.
    if latitude is None or longitude is None:
        return result

    observe = clamp_radius(radius_km) * OBSERVE_FACTOR
    coverage = coverage_fraction(grid, (latitude, longitude), observe)
    value = sample(grid, latitude, longitude)
    result.update({
        "coverage": round(coverage, 3),
        "observe_radius_km": round(observe, 1),
        "mmh_here": value,
        "peak_mmh": peak_in_disc(grid, (latitude, longitude),
                                 clamp_radius(radius_km)),
    })
    if value is None:
        result["status"] = "no_coverage"
        result["detail"] = "this point is outside the radar network"
    elif coverage < 0.4:
        result["status"] = "poor_coverage"
        result["detail"] = f"only {coverage:.0%} of the watched disc is visible"
    return result


@router.post("/api/ha/discover")
async def discover_ha_entities(prefix: str = Query(default="")):
    settings = load_settings()
    host     = settings.get("mqtt_host", "core-mosquitto")
    port     = int(settings.get("mqtt_port", 1883))
    username = settings.get("mqtt_username") or None
    password = settings.get("mqtt_password") or None
    prefix   = (prefix.strip().rstrip("/") if prefix.strip()
                else settings.get("mqtt_statestream_prefix", "homeassistant").rstrip("/"))

    return sorted(await _sniff_state_topics(host, port, username, password, prefix))
