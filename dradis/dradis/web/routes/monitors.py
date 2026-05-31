"""
web/routes/monitors.py
───────────────────────
Routes: scheduled monitors, live monitors, HA monitors, geocode, HA discovery.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import aiomqtt
import httpx
from fastapi import APIRouter, HTTPException

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
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": q, "count": 1, "language": "it", "format": "json"},
            )
        results = resp.json().get("results", [])
        if not results:
            raise HTTPException(status_code=404, detail=f"Location not found: {q!r}")
        r = results[0]
        return {
            "name":      r.get("name", q),
            "country":   r.get("country", ""),
            "latitude":  r["latitude"],
            "longitude": r["longitude"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Live Monitors ─────────────────────────────────────────────────────────────

@router.get("/api/live-monitors")
async def list_live_monitors():
    return load_live_monitors()


@router.post("/api/live-monitors")
async def create_live_monitor(payload: LiveMonitorPayload):
    if not payload.location.strip():
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
    if not payload.location.strip():
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


@router.post("/api/ha/discover")
async def discover_ha_entities():
    settings = load_settings()
    host     = settings.get("mqtt_host", "core-mosquitto")
    port     = int(settings.get("mqtt_port", 1883))
    username = settings.get("mqtt_username") or None
    password = settings.get("mqtt_password") or None
    prefix   = settings.get("mqtt_statestream_prefix", "homeassistant").rstrip("/")

    discovered: set[str] = set()
    kwargs = {}
    if username:
        kwargs["username"] = username
    if password:
        kwargs["password"] = password

    try:
        async with aiomqtt.Client(host, port, **kwargs) as client:
            await client.subscribe(f"{prefix}/+/+/state")
            deadline = asyncio.get_event_loop().time() + 3.0
            async for message in client.messages:
                topic  = str(message.topic)
                suffix = topic[len(prefix):].lstrip("/")
                if suffix.endswith("/state"):
                    entity_id = suffix[: -len("/state")]
                    discovered.add(entity_id)
                if asyncio.get_event_loop().time() >= deadline:
                    break
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MQTT discovery failed: {e}")

    return sorted(discovered)
