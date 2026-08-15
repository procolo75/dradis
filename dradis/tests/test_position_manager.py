"""
tests/test_position_manager.py
───────────────────────────────
The MQTT half of the position source: topic derivation, payload handling, dating
a retained message, per-position thresholds, and keeping several named positions
apart on one connection.

    cd dradis && python3 -m unittest discover tests

No broker is involved. Messages are handed to `_on_message` exactly as aiomqtt
would deliver them, which is the whole surface between the network and the pure
core.
"""

import sys
import time
import types
import unittest

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

from dradis.live_monitors.position import (               # noqa: E402
    PositionManager, PositionSource, _parse_timestamp, probe,
)

PREFIX = "homeassistant"
SETTINGS = {"mqtt_host": "core-mosquitto", "mqtt_port": 1883,
            "mqtt_statestream_prefix": PREFIX}

MINE = {
    "id": "p1", "name": "My phone",
    "lat_entity": "sensor/phone_latitude",
    "lon_entity": "sensor/phone_longitude",
    "accuracy_entity": "sensor/phone_gps_accuracy",
    "max_age_min": 15, "max_accuracy_m": 500,
}
THEIRS = {
    "id": "p2", "name": "Anna's phone",
    "lat_entity": "sensor/anna_latitude",
    "lon_entity": "sensor/anna_longitude",
    "accuracy_entity": "",
    "max_age_min": 60, "max_accuracy_m": 2000,
}


class Message:
    def __init__(self, topic: str, payload: str, retain: bool = False):
        self.topic = topic
        self.payload = payload.encode()
        self.retain = retain


def configured(*positions) -> PositionManager:
    """A manager holding `positions` but never started — `configure` would spawn
    the listener task, and there is no loop or broker here."""
    manager = PositionManager()
    manager._broker = PositionManager.broker_cfg(SETTINGS)
    sources = [PositionSource(p, PREFIX) for p in positions]
    manager._sources = {s.id: s for s in sources}
    manager._routes = {topic: (s.id, component)
                       for s in sources for topic, component in s.topics().items()}
    return manager


def deliver(manager, entity, payload, suffix="state", retain=False):
    manager._on_message(Message(f"{PREFIX}/{entity}/{suffix}", payload, retain))


def iso(offset_sec: float = 0.0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(time.time() + offset_sec))


class TopicTest(unittest.TestCase):

    def test_state_and_timestamp_topics_are_derived(self):
        topics = PositionSource(MINE, PREFIX).topics()
        self.assertEqual(topics[f"{PREFIX}/{MINE['lat_entity']}/state"], "lat")
        self.assertEqual(topics[f"{PREFIX}/{MINE['lon_entity']}/state"], "lon")
        self.assertEqual(topics[f"{PREFIX}/{MINE['lat_entity']}/last_updated"],
                         "lat_ts")

    def test_the_optional_accuracy_entity_is_skipped_when_unset(self):
        topics = PositionSource(THEIRS, PREFIX).topics()
        self.assertEqual(len(topics), 4)

    def test_a_per_position_prefix_override_wins(self):
        source = PositionSource({**MINE, "mqtt_prefix": "custom"}, PREFIX)
        self.assertIn(f"custom/{MINE['lat_entity']}/state", source.topics())


class MessageTest(unittest.TestCase):

    def setUp(self):
        self.manager = configured(MINE)

    def test_a_pair_of_coordinates_becomes_a_position(self):
        deliver(self.manager, MINE["lat_entity"], "40.82731")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        state = self.manager.current("p1")
        self.assertAlmostEqual(state.lat, 40.82731)
        self.assertAlmostEqual(state.lon, 14.13902)

    def test_accuracy_is_picked_up(self):
        deliver(self.manager, MINE["accuracy_entity"], "12")
        deliver(self.manager, MINE["lat_entity"], "40.82731")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        self.assertEqual(self.manager.current("p1").accuracy_m, 12.0)

    def test_unavailable_payloads_are_ignored(self):
        # A template sensor without an availability guard publishes these, and
        # float("unknown") would raise on every single message.
        for payload in ("unknown", "unavailable", "none", ""):
            with self.subTest(payload=payload):
                manager = configured(MINE)
                deliver(manager, MINE["lat_entity"], payload)
                self.assertIsNone(manager.current("p1"))

    def test_garbage_payloads_are_ignored(self):
        deliver(self.manager, MINE["lat_entity"], "not a number")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        self.assertIsNone(self.manager.current("p1"))

    def test_an_unrelated_topic_is_ignored(self):
        deliver(self.manager, "sensor/kitchen_temperature", "21.5")
        self.assertEqual(self.manager.stats()["messages"], 0)

    def test_a_retained_message_is_dated_by_last_updated(self):
        # The whole point: this arrives now but describes an hour ago, and
        # treating it as fresh is the failure that makes a stale position
        # indistinguishable from a live one.
        stamp = iso(-3600)
        deliver(self.manager, MINE["lat_entity"], stamp, "last_updated", True)
        deliver(self.manager, MINE["lon_entity"], stamp, "last_updated", True)
        deliver(self.manager, MINE["lat_entity"], "40.82731", retain=True)
        deliver(self.manager, MINE["lon_entity"], "14.13902", retain=True)
        self.assertGreater(self.manager.current("p1").age_sec, 3000)

    def test_without_a_timestamp_arrival_time_is_used(self):
        deliver(self.manager, MINE["lat_entity"], "40.82731")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        self.assertLess(self.manager.current("p1").age_sec, 5.0)

    def test_a_timestamp_from_the_future_is_not_trusted(self):
        stamp = iso(86400)
        deliver(self.manager, MINE["lat_entity"], stamp, "last_updated")
        deliver(self.manager, MINE["lon_entity"], stamp, "last_updated")
        deliver(self.manager, MINE["lat_entity"], "40.82731")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        # Clamped to now. A fix dated tomorrow would otherwise stay "fresh" for a
        # day, outliving every staleness check there is.
        self.assertLess(self.manager.current("p1").age_sec, 5.0)


class SeveralPositionsTest(unittest.TestCase):
    """Two phones on one connection. The failure to rule out is cross-talk: an
    alert about where somebody else is would be worse than no alert."""

    def setUp(self):
        self.manager = configured(MINE, THEIRS)

    def test_one_connection_serves_both(self):
        self.assertEqual(self.manager.stats()["positions"], 2)
        self.assertEqual(len(self.manager._routes), 6 + 4)

    def test_positions_do_not_contaminate_each_other(self):
        deliver(self.manager, MINE["lat_entity"], "40.82731")
        deliver(self.manager, MINE["lon_entity"], "14.13902")
        deliver(self.manager, THEIRS["lat_entity"], "45.46420")
        deliver(self.manager, THEIRS["lon_entity"], "9.18950")

        mine = self.manager.current("p1")
        theirs = self.manager.current("p2")
        self.assertAlmostEqual(mine.lat, 40.82731)
        self.assertAlmostEqual(theirs.lat, 45.46420)

    def test_only_the_position_that_reported_has_a_fix(self):
        deliver(self.manager, THEIRS["lat_entity"], "45.46420")
        deliver(self.manager, THEIRS["lon_entity"], "9.18950")
        self.assertIsNone(self.manager.current("p1"))
        self.assertIsNotNone(self.manager.current("p2"))

    def test_thresholds_are_per_position(self):
        self.assertEqual(self.manager.max_age_sec("p1"), 900.0)
        self.assertEqual(self.manager.max_age_sec("p2"), 3600.0)

    def test_a_fix_stale_for_one_is_fine_for_the_other(self):
        stamp = iso(-1800)                     # half an hour old
        for position in (MINE, THEIRS):
            deliver(self.manager, position["lat_entity"], stamp, "last_updated")
            deliver(self.manager, position["lon_entity"], stamp, "last_updated")
            deliver(self.manager, position["lat_entity"], "40.82731")
            deliver(self.manager, position["lon_entity"], "14.13902")
        self.assertIsNone(self.manager.usable("p1"))       # 15 min budget
        self.assertIsNotNone(self.manager.usable("p2"))    # 60 min budget

    def test_names_are_available_for_the_alert_header(self):
        self.assertEqual(self.manager.name_of("p1"), "My phone")
        self.assertEqual(self.manager.name_of("p2"), "Anna's phone")

    def test_an_unknown_position_answers_nothing_rather_than_raising(self):
        self.assertIsNone(self.manager.get("nope"))
        self.assertIsNone(self.manager.current("nope"))
        self.assertIsNone(self.manager.usable("nope"))
        self.assertIsNone(self.manager.name_of("nope"))


class UsableTest(unittest.TestCase):

    def _manager(self, age=0.0, accuracy="12"):
        manager = configured(MINE)
        stamp = iso(-age)
        if accuracy is not None:
            deliver(manager, MINE["accuracy_entity"], accuracy)
        deliver(manager, MINE["lat_entity"], stamp, "last_updated")
        deliver(manager, MINE["lon_entity"], stamp, "last_updated")
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        return manager

    def test_a_fresh_accurate_fix_is_usable(self):
        self.assertIsNotNone(self._manager(age=30).usable("p1"))

    def test_a_stale_fix_is_not(self):
        self.assertIsNone(self._manager(age=3600).usable("p1"))

    def test_a_stale_fix_is_still_readable(self):
        # `current` must keep returning it: the storm front decides for itself
        # how much patience an open event buys.
        self.assertIsNotNone(self._manager(age=3600).current("p1"))

    def test_a_caller_may_extend_the_budget(self):
        manager = self._manager(age=1200)
        self.assertIsNone(manager.usable("p1"))
        self.assertIsNotNone(manager.usable("p1", max_age_sec=2700.0))

    def test_an_imprecise_fix_is_not_usable(self):
        self.assertIsNone(self._manager(age=30, accuracy="5000").usable("p1"))

    def test_accuracy_is_not_required(self):
        self.assertIsNotNone(self._manager(age=30, accuracy=None).usable("p1"))


class ConfigureTest(unittest.TestCase):

    def test_no_position_never_starts(self):
        manager = PositionManager()
        manager.configure(SETTINGS, [])
        self.assertFalse(manager.is_running())
        self.assertEqual(manager.status(), "disabled")

    def test_an_incomplete_position_is_skipped(self):
        manager = PositionManager()
        manager.configure(SETTINGS, [{**MINE, "lon_entity": ""}])
        self.assertEqual(manager.ids(), [])
        self.assertFalse(manager.is_running())

    def test_renaming_keeps_the_fix_history(self):
        # Blinding a running monitor for the next few minutes because the user
        # fixed a typo in a name would be a poor trade.
        manager = configured(MINE)
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        self.assertIsNotNone(manager.current("p1"))

        manager.configure(SETTINGS, [{**MINE, "name": "Renamed"}])
        self.assertIsNotNone(manager.current("p1"))
        self.assertEqual(manager.name_of("p1"), "Renamed")

    def test_widening_a_threshold_keeps_the_fix_history(self):
        manager = configured(MINE)
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        manager.configure(SETTINGS, [{**MINE, "max_age_min": 60}])
        self.assertIsNotNone(manager.current("p1"))
        self.assertEqual(manager.max_age_sec("p1"), 3600.0)

    def test_changing_the_entities_drops_the_history(self):
        manager = configured(MINE)
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        manager.configure(SETTINGS, [{**MINE, "lat_entity": "sensor/other_lat"}])
        self.assertIsNone(manager.current("p1"))

    def test_adding_a_position_does_not_disturb_the_other(self):
        manager = configured(MINE)
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        manager.configure(SETTINGS, [MINE, THEIRS])
        self.assertIsNotNone(manager.current("p1"))
        self.assertEqual(sorted(manager.ids()), ["p1", "p2"])

    def test_removing_a_position_does_not_disturb_the_other(self):
        manager = configured(MINE, THEIRS)
        deliver(manager, MINE["lat_entity"], "40.82731")
        deliver(manager, MINE["lon_entity"], "14.13902")
        manager.configure(SETTINGS, [MINE])
        self.assertIsNotNone(manager.current("p1"))
        self.assertIsNone(manager.get("p2"))


class ProbeTest(unittest.TestCase):

    def test_a_probe_holds_exactly_the_posted_position(self):
        manager = probe(SETTINGS, {**MINE, "id": None})
        self.assertEqual(manager.ids(), ["probe"])
        self.assertIn(f"{PREFIX}/{MINE['lat_entity']}/state", manager._routes)

    def test_a_probe_is_a_separate_object(self):
        from dradis.live_monitors.position import position_manager
        self.assertIsNot(probe(SETTINGS, MINE), position_manager)


class TimestampParsingTest(unittest.TestCase):

    def test_iso_with_offset(self):
        self.assertIsNotNone(_parse_timestamp("2026-08-15T10:00:00+00:00"))

    def test_iso_with_z(self):
        self.assertIsNotNone(_parse_timestamp("2026-08-15T10:00:00Z"))

    def test_garbage_is_none(self):
        for raw in ("", "not a date", "12345"):
            with self.subTest(raw=raw):
                self.assertIsNone(_parse_timestamp(raw))


if __name__ == "__main__":
    unittest.main()
