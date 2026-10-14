"""
tests/test_assist.py
────────────────────
DRADIS as the conversation agent of Home Assistant's voice pipeline.

    cd dradis && python3 -m unittest discover tests

What is pinned down: HA Assist is the Telegram chat by another door. The same
`chat_turn` answers (same tools, same history), only the shape of the answer
changes — spoken prose — and side notices never reach the speaker. Plus the
add-on half of the wiring: the secret, the copy of the integration into HA's
config, the Supervisor discovery.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "dradis"))

import httpx                                                      # noqa: E402

import assist                                                     # noqa: E402
from tests.addon_import import import_bot_state                   # noqa: E402


class Token(unittest.TestCase):

    def test_created_once_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "assist_token")
            first = assist.ensure_token(path)
            self.assertGreater(len(first), 20)
            self.assertEqual(assist.ensure_token(path), first)


class Install(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = root / "src" / "dradis"
        self.src.mkdir(parents=True)
        (self.src / "manifest.json").write_text(json.dumps({"domain": "dradis", "version": "1.0.0"}))
        (self.src / "conversation.py").write_text("# v1")
        self.config = root / "config"
        self.config.mkdir()
        (self.config / "configuration.yaml").write_text("")

    def test_first_install_then_current_then_update(self):
        dest = self.config / "custom_components" / "dradis"
        self.assertEqual(assist.install_integration(self.src, self.config), "installed")
        self.assertEqual((dest / "conversation.py").read_text(), "# v1")
        self.assertEqual(assist.install_integration(self.src, self.config), "current")

        (self.src / "manifest.json").write_text(json.dumps({"domain": "dradis", "version": "1.0.1"}))
        (self.src / "conversation.py").write_text("# v2")
        self.assertEqual(assist.install_integration(self.src, self.config), "updated")
        self.assertEqual((dest / "conversation.py").read_text(), "# v2")

    def test_without_ha_config_nothing_is_written(self):
        self.assertIsNone(assist.ha_config_dir((Path(self.tmp.name) / "nope",)))
        with mock.patch.object(assist, "ha_config_dir", return_value=None):
            self.assertEqual(assist.install_integration(self.src), "no-config")

    def test_the_shipped_integration_is_complete(self):
        names = {p.name for p in assist.INTEGRATION_SRC.iterdir()}
        self.assertTrue({"manifest.json", "__init__.py", "config_flow.py",
                         "conversation.py", "const.py"} <= names)
        manifest = json.loads((assist.INTEGRATION_SRC / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], assist.DISCOVERY_SERVICE)
        self.assertTrue(manifest["config_flow"])


class Announce(unittest.IsolatedAsyncioTestCase):

    async def test_discovery_carries_url_and_token(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path,
                         json.loads(request.content) if request.content else None))
            if request.url.path == "/addons/self/info":
                return httpx.Response(200, json={"data": {"hostname": "local-dradis"}})
            return httpx.Response(200, json={"result": "ok", "data": {"uuid": "x"}})

        with mock.patch.dict("os.environ", {"SUPERVISOR_TOKEN": "sv"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                payload = await assist.announce("secret", 8099, client)
        self.assertEqual(payload, {"service": "dradis",
                                   "config": {"url": "http://local-dradis:8099", "token": "secret"}})
        self.assertEqual(seen[-1][:2], ("POST", "/discovery"))
        self.assertTrue(assist.STATUS["announced"])

    async def test_a_failed_discovery_does_not_raise(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(401))) as client:
            self.assertEqual(await assist.announce("secret", 8099, client), {})
        self.assertFalse(assist.STATUS["announced"])


class Endpoint(unittest.TestCase):
    """`/api/assist` through FastAPI, with the model replaced by a stub."""

    @classmethod
    def setUpClass(cls):
        cls.st = import_bot_state()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.routes import assist as route
        app = FastAPI()
        app.include_router(route.router)
        cls.route = route
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        token_file = Path(self.tmp.name, "assist_token")
        token_file.write_text("secret")
        self.patches = [
            mock.patch.object(assist, "TOKEN_FILE", token_file),
            mock.patch.object(assist.ensure_token, "__defaults__", (token_file,)),
            mock.patch.object(self.route, "load_settings", lambda: self.settings),
            mock.patch.object(self.st, "read_settings", lambda: self.settings),
            mock.patch.object(self.st, "send_telegram", self._sent_tg),
            mock.patch.object(self.st, "_send_error_telegram", self._sent_tg),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.settings = {"assist_enabled": True, "assist_mirror_telegram": False}
        self.telegram = []
        self.asked = []

    async def _sent_tg(self, text, *a, **k):
        self.telegram.append(text)

    def _turn(self, text="", error="", notices=()):
        async def fake(question, settings, source="chat", remember=True, channel=None):
            self.asked.append((question, source, remember))
            self.channel = channel
            return self.st.ChatTurn(text=text, error=error, notices=list(notices))
        return mock.patch.object(self.st, "chat_turn", fake)

    def post(self, body, token="secret"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.post("/api/assist", json=body, headers=headers)

    def test_no_or_wrong_token_is_refused(self):
        self.assertEqual(self.post({"text": "ciao"}, token=None).status_code, 401)
        self.assertEqual(self.post({"text": "ciao"}, token="nope").status_code, 401)

    def test_empty_text_is_refused(self):
        self.assertEqual(self.post({"text": "  "}).status_code, 400)

    def test_disabled_is_refused(self):
        self.settings["assist_enabled"] = False
        self.assertEqual(self.post({"text": "ciao"}).status_code, 503)

    def test_the_answer_is_the_chat_answer_spoken(self):
        with self._turn(text="**Domani** a Napoli 21°C 🌤️ https://example.com"):
            resp = self.post({"text": "che tempo fa domani?", "language": "it-IT",
                              "device_id": "dev1"})
        self.assertEqual(resp.status_code, 200)
        speech = resp.json()["speech"]
        self.assertIn("Domani", speech)
        for gone in ("**", "<b>", "🌤", "https://"):
            self.assertNotIn(gone, speech)
        self.assertEqual(self.asked, [("che tempo fa domani?", "assist", True)])
        self.assertEqual(self.telegram, [])
        self.assertEqual(self.channel, {"source": "assist", "device_id": "dev1", "language": "it"})

    def test_notices_are_never_spoken_and_go_to_telegram_only_with_the_mirror(self):
        with self._turn(text="Fatto.", notices=["⚠️ tool failure"]):
            speech = self.post({"text": "leggi le mail"}).json()["speech"]
        self.assertNotIn("tool", speech)
        self.assertEqual(self.telegram, [])

        self.settings["assist_mirror_telegram"] = True
        with self._turn(text="Fatto.", notices=["⚠️ tool failure"]):
            self.post({"text": "leggi le mail"})
        self.assertIn("leggi le mail", self.telegram[0])
        self.assertIn("⚠️ tool failure", self.telegram[1])

    def test_the_telegram_copy_carries_the_chat_footer(self):
        self.settings.update(assist_mirror_telegram=True, token_usage_enabled=True,
                             tools_usage_enabled=True)
        result = type("R", (), {"prompt_tokens": 1227, "completion_tokens": 74,
                                "tools_used": ["get_weather"]})()

        async def fake(question, settings, source="chat", remember=True, channel=None):
            return self.st.ChatTurn(text="Domani sole.", result=result)
        with mock.patch.object(self.st, "chat_turn", fake):
            speech = self.post({"text": "che tempo fa?"}).json()["speech"]
        self.assertIn("in 1227 · out 74", self.telegram[0])
        self.assertIn("get_weather", self.telegram[0])
        self.assertNotIn("1227", speech)

    def test_a_failed_model_says_a_short_sentence_and_reports_to_telegram(self):
        with self._turn(error="❌ Model error (<code>m</code>): boom"):
            speech = self.post({"text": "ciao"}).json()["speech"]
        self.assertNotIn("boom", speech)
        self.assertTrue(speech)
        self.assertIn("boom", self.telegram[0])

    def test_a_radar_command_skips_the_model(self):
        self.settings.update(control_tools_enabled=True, history_enabled=False,
                             assist_mirror_telegram=True, tools_usage_enabled=True)
        from bot import control

        async def fake_run(commands):
            self.ran = commands
            return "Pioggia casa: nessuna pioggia entro 30 km."
        with self._turn(text="non dovrei rispondere io"), \
             mock.patch("web.store.load_live_monitors",
                        lambda: [{"name": "Pioggia casa", "type": "rain_front"}]), \
             mock.patch.object(control, "run_radar_command", fake_run):
            speech = self.post({"text": "Cosa dice il radar?"}).json()["speech"]
        self.assertEqual(self.asked, [])                      # no model turn
        self.assertEqual(self.ran, [("rain", ""), ("storm", "")])
        self.assertIn("nessuna pioggia", speech)
        self.assertIn("radar_now (direct)", self.telegram[0])

    def test_a_stations_command_skips_the_model(self):
        self.settings.update(control_tools_enabled=True, history_enabled=False)
        from bot import control

        async def fake_stations(place=""):
            self.place = place
            return "Napoli: 18 gradi, nessuna pioggia."
        with self._turn(text="non dovrei rispondere io"), \
             mock.patch("web.store.load_live_monitors", lambda: []), \
             mock.patch.object(control, "station_readings", fake_stations):
            speech = self.post({"text": "misure a Napoli"}).json()["speech"]
        self.assertEqual(self.asked, [])
        self.assertEqual(self.place, "Napoli")
        self.assertIn("18 gradi", speech)

    def test_a_forecast_question_still_goes_to_the_model(self):
        self.settings["control_tools_enabled"] = True
        with self._turn(text="Domani sole."), \
             mock.patch("web.store.load_live_monitors", lambda: []):
            self.post({"text": "Piove domani?"})
        self.assertEqual(len(self.asked), 1)

    def test_status_checks_a_token_only_when_one_is_sent(self):
        self.assertEqual(self.client.get("/api/assist/status").status_code, 200)
        self.assertEqual(self.client.get("/api/assist/status",
                                         headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_the_token_is_shown_only_through_ingress(self):
        self.assertEqual(self.client.get("/api/assist/token").status_code, 403)
        resp = self.client.get("/api/assist/token", headers={"X-Ingress-Path": "/api/hassio_ingress/x"})
        self.assertEqual(resp.json()["token"], "secret")
        self.assertTrue(resp.json()["url"].startswith("http://"))


class SharedHistory(unittest.IsolatedAsyncioTestCase):
    """A question asked aloud is in the history the chat reads next."""

    async def test_an_echo_turn_lands_in_the_chat_history(self):
        st = import_bot_state()
        result = type("R", (), {"content": "Domani sole.", "tool_errors": [],
                                "prompt_tokens": 1, "completion_tokens": 1, "tools_used": []})()

        async def fake_run(*a, **k):
            return result, False, None, None

        st._history.clear()
        self.addCleanup(st._history.clear)
        with mock.patch.object(st, "run_dradis", fake_run):
            await st.chat_turn("che tempo fa domani?", {"history_enabled": True, "history_depth": 2},
                               source="assist")
            await st.chat_turn("prova", {"history_enabled": True, "history_depth": 2},
                               source="assist", remember=False)
        self.assertEqual([m["content"] for m in st.history_messages("x")],
                         ["che tempo fa domani?", "Domani sole."])


if __name__ == "__main__":
    unittest.main()
