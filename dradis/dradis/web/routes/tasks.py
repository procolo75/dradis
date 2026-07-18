"""
web/routes/tasks.py
────────────────────
Routes: scheduled task CRUD, cron validation, manual run trigger.
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException

import web.store as _store
from web.store import (
    _get_configured_tz,
    _validate_cron_expr,
    _notify_tasks_changed,
    available_tool_catalogue,
    load_settings,
    load_tasks,
    save_tasks,
)
from web.models import TaskPayload

router = APIRouter()


@router.get("/api/available-tools")
async def get_available_tools():
    """List the tools currently available (enabled + authenticated), so a task
    can select which ones to attach. DRADIS runs as one agent on the main model."""
    return available_tool_catalogue(load_settings())


@router.get("/api/tasks/validate-cron")
async def validate_cron(expr: str = ""):
    tz = _get_configured_tz()
    valid, error, next_fire = _validate_cron_expr(expr, tz)
    return {"valid": valid, "error": error, "next_fire": next_fire, "tz": tz}


@router.get("/api/tasks")
async def list_tasks():
    return load_tasks()


@router.post("/api/tasks")
async def create_task(payload: TaskPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    tasks = load_tasks()
    task = {
        "id":         str(uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **payload.model_dump(),
    }
    tasks.append(task)
    save_tasks(tasks)
    _notify_tasks_changed()
    return task


@router.put("/api/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskPayload):
    valid, error, _ = _validate_cron_expr(payload.cron)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {error}")
    tasks = load_tasks()
    for i, t in enumerate(tasks):
        if t["id"] == task_id:
            tasks[i] = {**t, **payload.model_dump()}
            save_tasks(tasks)
            _notify_tasks_changed()
            return tasks[i]
    raise HTTPException(status_code=404, detail="Task not found")


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    tasks = load_tasks()
    tasks = [t for t in tasks if t["id"] != task_id]
    save_tasks(tasks)
    _notify_tasks_changed()
    return {"ok": True}


@router.post("/api/tasks/{task_id}/run")
async def run_task_now(task_id: str):
    if not _store._run_task_fn:
        raise HTTPException(status_code=503, detail="Task runner not available")
    task = next((t for t in load_tasks() if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.get("instructions", "").strip():
        raise HTTPException(status_code=400, detail="Task has no instructions")
    asyncio.create_task(_store._run_task_fn(task))
    return {"ok": True}
