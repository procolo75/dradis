"""
web/routes/bots.py
──────────────────
CRUD endpoints for extra Telegram bots.
The default DRADIS bot (from config.yaml) is not stored here.
"""

import html
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from telegram import Bot

from web.models import BotPayload
from web.store import load_bots, save_bots, _notify_bots_changed

router = APIRouter()


@router.get("/api/bots")
async def list_bots():
    return load_bots()


@router.post("/api/bots")
async def create_bot(payload: BotPayload):
    bots = load_bots()
    new_bot = {
        "id":      str(uuid4()),
        "name":    payload.name,
        "token":   payload.token,
        "chat_id": payload.chat_id,
    }
    bots.append(new_bot)
    save_bots(bots)
    _notify_bots_changed()
    return new_bot


@router.put("/api/bots/{bot_id}")
async def update_bot(bot_id: str, payload: BotPayload):
    bots = load_bots()
    for i, b in enumerate(bots):
        if b["id"] == bot_id:
            bots[i] = {
                "id":      bot_id,
                "name":    payload.name,
                "token":   payload.token,
                "chat_id": payload.chat_id,
            }
            save_bots(bots)
            _notify_bots_changed()
            return bots[i]
    raise HTTPException(status_code=404, detail="Bot not found")


@router.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    bots = [b for b in load_bots() if b["id"] != bot_id]
    save_bots(bots)
    _notify_bots_changed()
    return {"ok": True}


@router.post("/api/bots/{bot_id}/test")
async def test_bot(bot_id: str):
    bots = load_bots()
    b = next((b for b in bots if b["id"] == bot_id), None)
    if not b:
        raise HTTPException(status_code=404, detail="Bot not found")
    try:
        bot = Bot(token=b["token"])
        me = await bot.get_me()
        await bot.send_message(
            chat_id=b["chat_id"],
            text=f"✅ DRADIS — test bot <b>{html.escape(me.username or me.first_name)}</b> connesso.",
            parse_mode="HTML",
        )
        return {"ok": True, "username": me.username}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
