"""
tests/test_control.py
─────────────────────
The Telegram menu as tools (bot/control.py), by voice or in the chat.

    cd dradis && python3 -m unittest discover tests

Pinned down: names are enums of what is configured (the model cannot invent
one), a task started by voice speaks its result on the device that asked, and
"turn it off" said twice does not turn it back on.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.addon_import import import_bot_state                   # noqa: E402

st = import_bot_state()
from bot import control                                           # noqa: E402

TASKS    = [{"id": "t1", "name": "Meteo mattina", "enabled": True, "instructions": "x"}]
MONITORS = [{"id": "m1", "name": "Grafici meteo", "enabled": True, "type": "weather_chart"}]
LIVE     = [{"id": "l1", "name": "Pioggia casa", "enabled": True, "type": "rain_front"},
            {"id": "l2", "name": "Calcio", "enabled": False, "type": "football_betting"}]
MONITORS_OFF = [{"id": "m2", "name": "Sismico", "enabled": False, "type": "seismic"}]


class Base(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.toggled = []
        patches = [
            mock.patch.object(control, "load_tasks", lambda: TASKS),
            mock.patch.object(control, "load_monitors", lambda: MONITORS),
            mock.patch.object(control, "load_live_monitors", lambda: LIVE),
            mock.patch.object(control, "load_settings", lambda: {"voice_language": "it"}),
            mock.patch.dict(control._KINDS, {
                "task": (lambda: TASKS, lambda i: self.toggled.append(("task", i))),
                "monitor": (lambda: MONITORS_OFF, lambda i: self.toggled.append(("monitor", i))),
            }),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Specs(Base):

    def test_names_are_enums_of_what_is_configured(self):
        specs = {s["name"]: s for s in control.control_tools({})}
        self.assertEqual(specs["run_task"]["parameters"]["properties"]["name"]["enum"],
                         ["Meteo mattina"])
        self.assertEqual(specs["radar_now"]["parameters"]["properties"]["name"]["enum"],
                         ["Pioggia casa"])
        self.assertEqual(specs["radar_now"]["parameters"]["required"], ["kind"])
        self.assertNotIn("monitor_status", specs)       # live monitors: no voice control
        self.assertEqual(specs["set_enabled"]["parameters"]["properties"]["kind"]["enum"],
                         ["task", "monitor", "car_mode"])
        self.assertNotIn("Calcio", specs["set_enabled"]["parameters"]["properties"]["name"]["enum"])
        self.assertNotIn("station_readings", specs)          # MeteoHub off
        self.assertIn("station_readings",
                      {s["name"] for s in control.control_tools({"meteohub_enabled": True})})

    def test_an_empty_list_leaves_the_tool_out(self):
        with mock.patch.object(control, "load_tasks", lambda: []):
            self.assertNotIn("run_task", {s["name"] for s in control.control_tools({})})

    def test_they_reach_the_model_through_build_tools(self):
        names = {t["name"] for t in st.build_tools({"control_tools_enabled": True}, None)}
        self.assertIn("run_task", names)
        names = {t["name"] for t in st.build_tools({"control_tools_enabled": False}, None)}
        self.assertNotIn("run_task", names)


class RunTask(Base):

    async def _run(self, channel):
        seen = {}

        async def fake_runner(task, on_result=None):
            seen["task"], seen["on_result"] = task, on_result

        token = st.CHANNEL.set(channel)
        try:
            with mock.patch("bot.scheduler.run_scheduled_task", fake_runner):
                out = await control.run_task("Meteo mattina")
                await asyncio.sleep(0)
        finally:
            st.CHANNEL.reset(token)
        return out, seen

    async def test_from_the_chat_it_runs_as_the_menu_does(self):
        out, seen = await self._run({"source": "chat"})
        self.assertEqual(seen["task"]["id"], "t1")
        self.assertIsNone(seen["on_result"])
        self.assertIn("started", out)

    async def test_by_voice_the_result_is_spoken_on_the_asking_device(self):
        out, seen = await self._run({"source": "assist", "device_id": "dev1", "language": "it"})
        spoken = []

        async def fake_speak(text, lang, device_id, settings):
            spoken.append((text, lang, device_id))
        with mock.patch.object(control.speaker, "speak", fake_speak):
            await seen["on_result"]("Domani sole.")
        self.assertEqual(spoken, [("Domani sole.", "it", "dev1")])
        self.assertIn("spoken here", out)

    async def test_an_unknown_name_is_said_plainly(self):
        self.assertIn("No task", await control.run_task("Boh"))


class SetEnabled(Base):

    async def test_an_equal_state_is_not_toggled(self):
        await control.set_enabled("task", True, "Meteo mattina")      # already on
        await control.set_enabled("monitor", False, "Sismico")        # already off
        self.assertEqual(self.toggled, [])

    async def test_a_different_state_is_toggled_once(self):
        out = await control.set_enabled("monitor", True, "Sismico")
        self.assertEqual(self.toggled, [("monitor", "m2")])
        self.assertIn("enabled", out)

    async def test_a_live_monitor_cannot_be_switched_by_voice(self):
        self.assertIn("Unknown kind", await control.set_enabled("live_monitor", False, "Calcio"))

    async def test_car_mode_needs_no_name(self):
        with mock.patch.object(control, "set_car_mode") as car:
            out = await control.set_enabled("car_mode", True)
        car.assert_called_once_with(True)
        self.assertIn("on", out)


class LiveMonitorVoice(unittest.IsolatedAsyncioTestCase):
    """A live monitor with "Announce by voice" says its alert too; one without
    stays Telegram-only. Telegram's delivery status is what the monitor gets."""

    async def test_speak_alerts_adds_the_speaker_without_touching_delivery(self):
        import bot.scheduler as sch
        factories = []
        with mock.patch.object(sch.storm_front_monitor_manager, "reload",
                               lambda configs, make_send, tz: factories.append(make_send)), \
             mock.patch.object(sch, "load_live_monitors", lambda: []):
            sch.reload_live_monitors()
        make_send = factories[0]

        said, sent = [], []

        async def fake_speak(text, lang, device_id, settings):
            said.append((text, lang, device_id))

        async def fake_tg(text, bot_id="default", lang="it"):
            sent.append(text)
            return st.DELIVERED

        with mock.patch.object(sch.speaker, "speak", fake_speak), \
             mock.patch.object(st, "send_telegram", fake_tg):
            quiet = make_send({"language": "it"})
            loud = make_send({"language": "it", "speak_alerts": True})
            self.assertEqual(await quiet("Temporale a 10 km"), st.DELIVERED)
            self.assertEqual(await loud("Temporale a 5 km"), st.DELIVERED)
            await asyncio.sleep(0)
        self.assertEqual(sent, ["Temporale a 10 km", "Temporale a 5 km"])
        self.assertEqual(said, [("Temporale a 5 km", "it", None)])

        said.clear(); sent.clear()
        with mock.patch.object(sch.speaker, "speak", fake_speak), \
             mock.patch.object(st, "send_telegram", fake_tg), \
             mock.patch.object(sch.speaker, "in_quiet_hours", lambda *a, **k: True):
            night = make_send({"language": "it", "speak_alerts": True,
                               "speak_quiet_start": "23:00", "speak_quiet_end": "07:00"})
            self.assertEqual(await night("Temporale a 3 km"), st.DELIVERED)
            await asyncio.sleep(0)
        self.assertEqual((sent, said), (["Temporale a 3 km"], []))


class Radar(Base):
    """Two rain monitors and no name: both answer, by name. With a name: one."""

    async def test_without_a_name_every_monitor_of_the_kind_answers(self):
        live = LIVE + [{"id": "l3", "name": "Pioggia ufficio", "type": "rain_front"}]
        async def fake_take(kind, cfg):
            return type("S", (), {"picture": None, "name": cfg["name"]})()
        token = st.CHANNEL.set({"source": "assist", "language": "it"})
        try:
            with mock.patch.object(control, "load_live_monitors", lambda: live), \
                 mock.patch("bot.handlers._take_snapshot", fake_take), \
                 mock.patch("live_monitors.snapshot.format_caption",
                            lambda s, voice: f"{s.name}, situazione ora. Nessuna pioggia."):
                both = await control.radar_now("rain")
                one = await control.radar_now("rain", "Pioggia ufficio")
        finally:
            st.CHANNEL.reset(token)
        self.assertEqual(both.count("Pioggia casa"), 1)          # said once, not twice
        self.assertEqual(both.count("Pioggia ufficio"), 1)
        self.assertNotIn("Pioggia casa", one)
        self.assertIn("Nessuna pioggia", one)

    async def test_the_radar_rule_reaches_the_prompt_only_with_the_tool(self):
        s = {"timezone": "UTC", "agent_instructions": "x"}
        with mock.patch.object(st, "read_settings", lambda: s):
            self.assertIn("call radar_now", st._system_prompt(s, [{"name": "radar_now"}]))
            self.assertNotIn("radar_now", st._system_prompt(s, []))


class RadarWords(unittest.TestCase):
    """The words agreed with the user, sentence by sentence."""

    MONS = [{"name": "Temporali", "type": "storm_front"},
            {"name": "Temporali_2", "type": "storm_front"},
            {"name": "Pioggia casa", "type": "rain_front"},
            {"name": "Pioggia", "type": "rain"}]                # a SCHEDULED monitor

    def m(self, text):
        return control.match_radar(text, self.MONS)

    def test_the_agreed_table(self):
        self.assertEqual(self.m("Rain"), [("rain", "")])
        self.assertEqual(self.m("Lancia il comando rain"), [("rain", "")])
        self.assertEqual(self.m("Storm temporali 2"), [("storm", "Temporali_2")])
        self.assertEqual(self.m("Cosa dice il radar?"), [("rain", ""), ("storm", "")])
        self.assertEqual(self.m("Radar pioggia"), [("rain", "")])
        self.assertEqual(self.m("Il radar dice che piove?"), [("rain", "")])
        # "Temporali" is also a monitor's name, but a name made of trigger words
        # does not narrow: the user asked for the kind.
        self.assertEqual(self.m("Radar temporali"), [("storm", "")])
        self.assertEqual(self.m("Ci sono fulmini sul radar?"), [("storm", "")])
        self.assertEqual(self.m("Sta per piovere?"), [("rain", "")])      # added by the user
        self.assertEqual(self.m("Sta per piovere a casa?"), [("rain", "")])
        self.assertEqual(self.m("Piove domani?"), [])

    def test_a_name_narrows_only_its_own_kind(self):
        self.assertEqual(self.m("rain pioggia casa"), [("rain", "Pioggia casa")])

    def test_accents_and_punctuation_do_not_matter(self):
        self.assertEqual(self.m("RADAR... temporàli 2!"), [("storm", "Temporali_2")])


class VoiceQuietHours(unittest.TestCase):

    def test_windows(self):
        from datetime import time as t
        import speaker
        self.assertTrue(speaker.in_quiet_hours("23:00", "07:00", now=t(3, 0)))
        self.assertFalse(speaker.in_quiet_hours("23:00", "07:00", now=t(7, 0)))
        self.assertTrue(speaker.in_quiet_hours("13:00", "15:00", now=t(14, 30)))
        self.assertFalse(speaker.in_quiet_hours("", "07:00", now=t(3, 0)))
        self.assertFalse(speaker.in_quiet_hours("xx", "07:00", now=t(3, 0)))


class StationWords(unittest.TestCase):
    """/stations by voice: misure, stazioni, sensori; a place after a preposition."""

    def test_the_agreed_words(self):
        m = control.match_stations
        self.assertEqual(m("Misure meteo"), "")
        self.assertEqual(m("Cosa dicono i sensori?"), "")
        self.assertEqual(m("misure qui"), "")
        self.assertEqual(m("misure a Napoli"), "Napoli")
        self.assertEqual(m("sensori a Bacoli adesso"), "Bacoli")
        self.assertEqual(m("stazioni meteo vicino a Pozzuoli"), "Pozzuoli")
        self.assertEqual(m("stazioni di Monte di Procida"), "Monte di Procida")
        self.assertIsNone(m("Che tempo fa domani?"))

    def test_singulars_too(self):
        m = control.match_stations
        self.assertEqual(m("Dammi i dati della stazione meteo"), "")
        self.assertEqual(m("che dice il sensore a Bacoli?"), "Bacoli")
        self.assertEqual(m("una misura a Napoli"), "Napoli")


if __name__ == "__main__":
    unittest.main()
