"""
web/routes/agents.py
─────────────────────
Routes: agent CRUD, model listing, speedtest, voice models.
"""

import asyncio
import time

import httpx
from fastapi import APIRouter, HTTPException

from web.store import (
    GITHUB_MODELS,
    GEMINI_MODELS,
    _SPEEDTEST_PROMPT,
    _SPEEDTEST_MAX_TOKENS,
    _SPEEDTEST_TOP_N,
    _get_provider_api_key,
    _get_provider_base_url,
    _parse_size_b,
    load_agents,
    save_agents,
)
from web.models import AgentPayload, SpeedtestPayload

router = APIRouter()


# ── Agent CRUD ────────────────────────────────────────────────────────────────

@router.get("/api/agents")
async def list_agents():
    return load_agents()


@router.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, payload: AgentPayload):
    agents = load_agents()
    for i, a in enumerate(agents):
        if a["id"] == agent_id:
            agents[i] = {**a, **payload.model_dump()}
            save_agents(agents)
            return agents[i]
    raise HTTPException(status_code=404, detail="Agent not found")


# ── Model fetch helpers ───────────────────────────────────────────────────────

async def _fetch_openrouter_models(api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        model_id = m.get("id", "")
        pricing  = m.get("pricing", {})
        is_free  = (
            model_id.endswith(":free") or
            (pricing.get("prompt") == "0" and pricing.get("completion") == "0")
        )
        if not is_free:
            continue
        if "tools" not in m.get("supported_parameters", []):
            continue
        size = _parse_size_b(m)
        if size < 30:
            continue
        result.append({
            "id":             model_id,
            "name":           m.get("name", model_id),
            "size_b":         size,
            "context_length": m.get("context_length", 0),
        })
    result.sort(key=lambda x: x["size_b"])
    return result


async def _fetch_openai_models(api_key: str) -> list[dict]:
    _OPENAI_TOOL_MODELS = {
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4-turbo-preview",
        "gpt-3.5-turbo", "gpt-4", "gpt-4-0125-preview",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if any(mid.startswith(k) for k in _OPENAI_TOOL_MODELS):
            result.append({"id": mid, "name": mid})
    result.sort(key=lambda x: x["id"])
    return result


async def _fetch_groq_models(api_key: str) -> list[dict]:
    _GROQ_EXCLUDE = ("whisper", "distil-", "guard")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if any(mid.startswith(ex) or ex in mid for ex in _GROQ_EXCLUDE):
            continue
        result.append({"id": mid, "name": m.get("id", mid)})
    result.sort(key=lambda x: x["id"])
    return result


async def _fetch_groq_voice_models(api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    result = [
        {"id": m["id"], "name": m["id"]}
        for m in resp.json().get("data", [])
        if "whisper" in m.get("id", "")
    ]
    result.sort(key=lambda x: x["id"])
    return result


# ── Model listing endpoints ───────────────────────────────────────────────────

@router.get("/api/models")
async def get_models(provider: str = "openrouter"):
    api_key = _get_provider_api_key(provider)
    try:
        if provider == "openrouter":
            return await _fetch_openrouter_models(api_key)
        elif provider == "openai":
            return await _fetch_openai_models(api_key)
        elif provider == "github":
            return GITHUB_MODELS
        elif provider == "gemini":
            return GEMINI_MODELS
        elif provider == "groq":
            return await _fetch_groq_models(api_key)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/openrouter/models")
async def get_openrouter_models():
    return await get_models("openrouter")


@router.get("/api/voice-models")
async def get_voice_models():
    api_key = _get_provider_api_key("groq")
    if not api_key:
        raise HTTPException(status_code=400, detail="Groq API key not configured in add-on settings")
    try:
        return await _fetch_groq_voice_models(api_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/voice-test")
async def test_voice():
    api_key = _get_provider_api_key("groq")
    if not api_key:
        raise HTTPException(status_code=400, detail="Groq API key not configured in add-on settings")
    try:
        models = await _fetch_groq_voice_models(api_key)
        if models:
            return {"ok": True, "message": f"Groq reachable — {len(models)} Whisper model(s) available"}
        return {"ok": False, "message": "Groq reachable but no Whisper models found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Speedtest ─────────────────────────────────────────────────────────────────

@router.post("/api/speedtest")
async def run_speedtest(payload: SpeedtestPayload, provider: str = "openrouter"):
    api_key  = _get_provider_api_key(provider)
    base_url = _get_provider_base_url(provider)
    sem      = asyncio.Semaphore(4)

    async def test_one(model_id: str) -> dict:
        async with sem:
            t0 = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type":  "application/json",
                            "X-Title":       "DRADIS-speedtest",
                        },
                        json={
                            "model":      model_id,
                            "messages":   [{"role": "user", "content": _SPEEDTEST_PROMPT}],
                            "max_tokens": _SPEEDTEST_MAX_TOKENS,
                        },
                    )
                elapsed = time.monotonic() - t0
                if resp.status_code == 200:
                    data = resp.json()
                    token_out = (
                        data.get("usage", {}).get("completion_tokens") or
                        len((data.get("choices") or [{}])[0]
                            .get("message", {}).get("content", "").split())
                    )
                    tok_s = round(token_out / elapsed, 1) if elapsed > 0 and token_out else None
                    return {"id": model_id, "tok_s": tok_s, "ok": tok_s is not None}
                return {"id": model_id, "tok_s": None, "ok": False}
            except Exception:
                return {"id": model_id, "tok_s": None, "ok": False}

    all_results = await asyncio.gather(*[test_one(m) for m in payload.models])
    successful  = sorted(
        [r for r in all_results if r["ok"]],
        key=lambda x: x["tok_s"],
        reverse=True,
    )
    failed = [r for r in all_results if not r["ok"]]
    return (successful + failed)[:_SPEEDTEST_TOP_N]


@router.post("/api/openrouter/speedtest")
async def run_speedtest_legacy(payload: SpeedtestPayload):
    return await run_speedtest(payload, provider="openrouter")
