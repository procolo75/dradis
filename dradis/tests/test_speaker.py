"""
tests/test_speaker.py
─────────────────────
DRADIS speaking on a Home Assistant voice assistant, outside a reply.

    cd dradis && python3 -m unittest discover tests

No HA here: a MockTransport plays the Supervisor's core API proxy.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

import httpx                                                      # noqa: E402

import speaker                                                    # noqa: E402


def _ha(calls: list, device_entities: str = ""):
    def handler(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.path, body))
        if request.url.path.endswith("/template"):
            return httpx.Response(200, text=device_entities)
        return httpx.Response(200, json=[])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Trim(unittest.TestCase):

    def test_short_text_is_only_made_speakable(self):
        self.assertEqual(speaker.trim_for_speech("<b>Ciao</b> 🌤️"), "Ciao.")

    def test_long_text_is_cut_at_a_sentence_end(self):
        text = "Prima frase. " * 80
        out = speaker.trim_for_speech(text, "it", limit=100)
        self.assertTrue(out.endswith("Il resto è su Telegram."))
        self.assertLessEqual(len(out), 100 + len(" Il resto è su Telegram."))
        self.assertIn("Prima frase. Prima frase.", out)
        self.assertFalse(out.replace(" Il resto è su Telegram.", "").endswith("Prima"))


class Speak(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        p = mock.patch.dict("os.environ", {"SUPERVISOR_TOKEN": "sv"})
        p.start()
        self.addCleanup(p.stop)

    async def test_the_asking_device_announces_on_its_satellite(self):
        calls = []
        async with _ha(calls, "media_player.echo,assist_satellite.echo,sensor.x") as client:
            target = await speaker.speak("Fatto.", "it", "dev1", {}, client)
        self.assertEqual(target, "assist_satellite.echo")
        self.assertIn("dev1", calls[0][1]["template"])
        self.assertEqual(calls[-1], ("/core/api/services/assist_satellite/announce",
                                     {"entity_id": "assist_satellite.echo", "message": "Fatto."}))

    async def test_a_media_player_speaks_through_the_tts_engine(self):
        calls = []
        settings = {"assist_speaker_entity": "media_player.echo",
                    "assist_tts_entity": "tts.piper"}
        async with _ha(calls) as client:
            target = await speaker.speak("Fatto.", "it", None, settings, client)
        self.assertEqual(target, "media_player.echo")
        self.assertEqual(calls[-1], ("/core/api/services/tts/speak",
                                     {"entity_id": "tts.piper",
                                      "media_player_entity_id": "media_player.echo",
                                      "message": "Fatto."}))

    async def test_a_media_player_without_tts_says_nothing(self):
        calls = []
        async with _ha(calls) as client:
            target = await speaker.speak("Fatto.", "it", None,
                                         {"assist_speaker_entity": "media_player.echo"}, client)
        self.assertEqual((target, calls), ("", []))

    async def test_no_device_and_no_default_says_nothing(self):
        calls = []
        async with _ha(calls) as client:
            self.assertEqual(await speaker.speak("Fatto.", "it", None, {}, client), "")
        self.assertEqual(calls, [])

    async def test_an_unknown_device_falls_back_to_the_default(self):
        calls = []
        async with _ha(calls, "sensor.only") as client:
            target = await speaker.speak("Fatto.", "it", "dev1",
                                         {"assist_speaker_entity": "assist_satellite.kitchen"}, client)
        self.assertEqual(target, "assist_satellite.kitchen")

    async def test_the_speaking_call_waits_for_the_speech_to_end(self):
        timeouts = []

        def handler(request):
            timeouts.append((request.url.path, request.extensions.get("timeout", {}).get("read")))
            return httpx.Response(200, text="assist_satellite.echo")
        async with httpx.AsyncClient(timeout=15, transport=httpx.MockTransport(handler)) as client:
            await speaker.speak("Lista lunga.", "it", "dev1", {}, client)
        self.assertEqual(timeouts[-1], ("/core/api/services/assist_satellite/announce",
                                        speaker._SPEAK_TIMEOUT))
        self.assertGreaterEqual(speaker._SPEAK_TIMEOUT, 120)

    async def test_waits_for_the_satellite_to_finish_its_reply(self):
        states = iter(["responding", "responding", "idle"])
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if "/states/" in request.url.path:
                return httpx.Response(200, json={"state": next(states)})
            return httpx.Response(200, json=[])
        slept = []

        async def fake_sleep(sec):
            slept.append(sec)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ok = await speaker.wait_until_idle(client, "assist_satellite.echo", sleep=fake_sleep)
        self.assertTrue(ok)
        self.assertEqual(len(slept), 2)

    async def test_a_satellite_that_never_settles_is_spoken_to_anyway(self):
        def handler(request):
            return httpx.Response(200, json={"state": "responding"})

        async def fake_sleep(sec):
            pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ok = await speaker.wait_until_idle(client, "assist_satellite.echo", max_wait=3,
                                               poll=1, sleep=fake_sleep)
        self.assertFalse(ok)

    async def test_the_announcement_follows_the_wait(self):
        order = []

        def handler(request):
            order.append(request.url.path)
            if request.url.path.endswith("/template"):
                return httpx.Response(200, text="assist_satellite.echo")
            if "/states/" in request.url.path:
                return httpx.Response(200, json={"state": "idle"})
            return httpx.Response(200, json=[])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await speaker.speak("Fatto.", "it", "dev1", {}, client)
        self.assertEqual(order, ["/core/api/template", "/core/api/states/assist_satellite.echo",
                                 "/core/api/services/assist_satellite/announce"])

    async def test_an_ha_error_never_raises(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(500))) as client:
            self.assertEqual(await speaker.speak("Fatto.", "it", "dev1",
                                                 {"assist_speaker_entity": "assist_satellite.x"},
                                                 client), "")

    async def test_entities_for_the_panel(self):
        states = [{"entity_id": "media_player.echo", "attributes": {"friendly_name": "Echo"}},
                  {"entity_id": "assist_satellite.echo", "attributes": {"friendly_name": "Echo sat"}},
                  {"entity_id": "tts.piper", "attributes": {"friendly_name": "Piper"}},
                  {"entity_id": "light.x", "attributes": {}}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json=states))) as client:
            ents = await speaker.list_entities(client)
        self.assertEqual([e["id"] for e in ents["speakers"]],
                         ["assist_satellite.echo", "media_player.echo"])
        self.assertEqual([e["id"] for e in ents["tts"]], ["tts.piper"])


class Token(unittest.TestCase):

    def test_read_from_s6_when_the_environment_was_cleaned(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            Path(d, "SUPERVISOR_TOKEN").write_text("from-s6\n")
            with mock.patch.dict("os.environ", {}, clear=True), \
                 mock.patch.object(speaker, "_S6_ENV_DIR", d):
                self.assertEqual(speaker.supervisor_token(), "from-s6")


if __name__ == "__main__":
    unittest.main()
