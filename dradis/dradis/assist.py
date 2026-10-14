"""
assist.py
─────────
DRADIS as a conversation agent in Home Assistant's voice pipeline.

A voice assistant (any Assist satellite) talks to HA. With "Prefer handling commands
locally" on, HA's own intents take "turn off the living room light" — instantly,
with its exact name and alias matching — and only what HA does not understand
reaches the pipeline's conversation agent: DRADIS, through the small HA
integration shipped in `ha_integration/dradis`.

This module is the add-on's half of that wiring, and it needs no input from the
user: it keeps a shared secret, copies the integration into HA's config, and
announces itself to the Supervisor so HA offers "Discovered: DRADIS" with the URL
and the secret already filled in. The endpoint the integration calls lives in
`web/routes/assist.py`.

No telegram or LLM imports here: the install and discovery logic is tested on
its own (`tests/test_assist.py`).
"""

import json
import secrets
import shutil
from pathlib import Path

import httpx

from speaker import supervisor_token

TOKEN_FILE      = Path("/data/assist_token")
INTEGRATION_SRC = Path(__file__).resolve().parent.parent / "ha_integration" / "dradis"
# `homeassistant_config` is mapped at /homeassistant; /config is the legacy name.
HA_CONFIG_DIRS  = (Path("/homeassistant"), Path("/config"))
SUPERVISOR      = "http://supervisor"
DISCOVERY_SERVICE = "dradis"

# What the last install / announce / request did — for the Web UI and logs.
STATUS: dict = {
    "installed_version": "", "install": "", "announced": False, "announce_error": "",
    "last_request": None,
}


def ensure_token(path: Path = TOKEN_FILE) -> str:
    """The secret the HA integration sends as a Bearer token. Created once and
    kept: changing it would silently break an integration already set up."""
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.write_text(token)
    return token


def _version(manifest_dir: Path) -> str:
    try:
        return json.loads((manifest_dir / "manifest.json").read_text()).get("version", "")
    except (OSError, ValueError):
        return ""


def ha_config_dir(candidates=HA_CONFIG_DIRS) -> Path | None:
    for d in candidates:
        if (d / "configuration.yaml").exists():
            return d
    return None


def install_integration(src: Path = INTEGRATION_SRC, config_dir: Path | None = None) -> str:
    """Copy the integration into HA's custom_components when it is missing or a
    different version. Returns "installed", "updated", "current" or "no-config"
    (HA's config folder is not mapped). HA loads it only after a restart."""
    config_dir = config_dir or ha_config_dir()
    if config_dir is None:
        STATUS.update(install="no-config")
        return "no-config"
    dest = config_dir / "custom_components" / src.name
    wanted, present = _version(src), _version(dest)
    if dest.exists() and present == wanted:
        STATUS.update(install="current", installed_version=present)
        return "current"
    outcome = "updated" if dest.exists() else "installed"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    STATUS.update(install=outcome, installed_version=wanted)
    print(f"[DRADIS] Assist: HA integration {outcome} ({wanted}) at {dest}")
    return outcome


async def announce(token: str, port: int, client: httpx.AsyncClient | None = None) -> dict:
    """Tell the Supervisor where DRADIS answers, so HA can offer it as a
    discovered integration. Never raises: the add-on works without it, and the
    integration can still be added by hand with the URL and token."""
    headers = {"Authorization": f"Bearer {supervisor_token()}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        info = await client.get(f"{SUPERVISOR}/addons/self/info", headers=headers)
        info.raise_for_status()
        hostname = info.json()["data"]["hostname"]
        payload = {"service": DISCOVERY_SERVICE,
                   "config": {"url": f"http://{hostname}:{port}", "token": token}}
        resp = await client.post(f"{SUPERVISOR}/discovery", json=payload, headers=headers)
        resp.raise_for_status()
        STATUS.update(announced=True, announce_error="")
        print(f"[DRADIS] Assist: announced to Home Assistant at http://{hostname}:{port}")
        return payload
    except Exception as e:
        STATUS.update(announced=False, announce_error=str(e))
        print(f"[DRADIS] Assist: discovery failed: {e}")
        return {}
    finally:
        if own:
            await client.aclose()


async def startup(port: int, notify=None) -> None:
    """Token, integration, discovery — in that order, each failure logged and
    contained. `notify(text)` is awaited once when HA must be restarted to load
    a newly copied integration."""
    try:
        token = ensure_token()
    except OSError as e:
        print(f"[DRADIS] Assist: cannot store the token: {e}")
        return
    try:
        outcome = install_integration()
    except OSError as e:
        outcome = ""
        STATUS.update(install=f"error: {e}")
        print(f"[DRADIS] Assist: cannot install the HA integration: {e}")
    if outcome in ("installed", "updated") and notify:
        await notify("🎙️ HA Assist: DRADIS integration for Home Assistant "
                     f"{'installed' if outcome == 'installed' else 'updated'}. "
                     "Restart Home Assistant to load it, then add it in "
                     "Settings → Devices & services → Add integration → DRADIS.")
    await announce(token, port)
