"""
tests/test_position_migration.py
─────────────────────────────────
The settings half of the move from one global position to a named list.

    cd dradis && python3 -m unittest discover tests

The monitor half lives in test_storm_front_position.py; the two must agree, since
a position migrated without its monitors — or monitors migrated without their
position — leaves a monitor pointing at something that does not exist, frozen
with no visible cause.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

from dradis.web import store                                        # noqa: E402

LEGACY = {
    "timezone": "Europe/Rome",
    "mqtt_host": "core-mosquitto",
    "position_enabled": True,
    "position_lat_entity": "sensor/phone_latitude",
    "position_lon_entity": "sensor/phone_longitude",
    "position_accuracy_entity": "sensor/phone_gps_accuracy",
    "position_max_age_min": 20.0,
    "position_max_accuracy_m": 300.0,
    "position_mqtt_prefix": "",
}


class MigrationTest(unittest.TestCase):

    def setUp(self):
        self._original = store.SETTINGS_FILE
        self.path = Path(tempfile.mktemp(suffix="-settings.json"))
        store.SETTINGS_FILE = self.path

    def tearDown(self):
        store.SETTINGS_FILE = self._original
        self.path.unlink(missing_ok=True)

    def write(self, data: dict):
        self.path.write_text(json.dumps(data))

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def test_the_saved_position_becomes_a_named_one(self):
        self.write(LEGACY)
        new_id = store.migrate_legacy_position()

        self.assertIsNotNone(new_id)
        positions = store.load_positions()
        self.assertEqual(len(positions), 1)
        position = positions[0]
        self.assertEqual(position["id"], new_id)
        self.assertEqual(position["name"], "My phone")
        self.assertEqual(position["lat_entity"], "sensor/phone_latitude")
        self.assertEqual(position["lon_entity"], "sensor/phone_longitude")
        self.assertEqual(position["accuracy_entity"], "sensor/phone_gps_accuracy")

    def test_the_thresholds_come_along(self):
        # Silently resetting them would change how the monitor behaves for a user
        # who had deliberately widened them.
        self.write(LEGACY)
        store.migrate_legacy_position()
        position = store.load_positions()[0]
        self.assertEqual(position["max_age_min"], 20.0)
        self.assertEqual(position["max_accuracy_m"], 300.0)

    def test_the_legacy_keys_are_removed(self):
        self.write(LEGACY)
        store.migrate_legacy_position()
        raw = self.read()
        for key in store._LEGACY_POSITION_KEYS:
            self.assertNotIn(key, raw)

    def test_unrelated_settings_are_untouched(self):
        self.write(LEGACY)
        store.migrate_legacy_position()
        raw = self.read()
        self.assertEqual(raw["timezone"], "Europe/Rome")
        self.assertEqual(raw["mqtt_host"], "core-mosquitto")

    def test_it_is_idempotent(self):
        self.write(LEGACY)
        store.migrate_legacy_position()
        self.assertIsNone(store.migrate_legacy_position())
        self.assertEqual(len(store.load_positions()), 1)

    def test_nothing_to_migrate_returns_none(self):
        self.write({"timezone": "UTC"})
        self.assertIsNone(store.migrate_legacy_position())
        self.assertEqual(store.load_positions(), [])

    def test_an_incomplete_legacy_config_creates_no_position(self):
        # Half a configuration is not a position, and creating one would put an
        # unusable entry in the list for the user to wonder about.
        self.write({**LEGACY, "position_lon_entity": ""})
        self.assertIsNone(store.migrate_legacy_position())
        self.assertEqual(store.load_positions(), [])
        self.assertNotIn("position_lat_entity", self.read())

    def test_an_existing_named_position_is_preserved(self):
        self.write({**LEGACY, "positions": [{"id": "keep", "name": "Anna"}]})
        store.migrate_legacy_position()
        ids = [p["id"] for p in store.load_positions()]
        self.assertIn("keep", ids)
        self.assertEqual(len(ids), 2)

    def test_a_missing_settings_file_is_not_an_error(self):
        self.assertIsNone(store.migrate_legacy_position())


class RoundTripTest(unittest.TestCase):

    def setUp(self):
        self._original = store.SETTINGS_FILE
        self.path = Path(tempfile.mktemp(suffix="-settings.json"))
        store.SETTINGS_FILE = self.path
        self.path.write_text(json.dumps({"timezone": "UTC"}))

    def tearDown(self):
        store.SETTINGS_FILE = self._original
        self.path.unlink(missing_ok=True)

    def test_positions_survive_a_save_load_cycle(self):
        store.save_positions([{"id": "p1", "name": "My phone"}])
        self.assertEqual(store.load_positions()[0]["name"], "My phone")

    def test_saving_positions_does_not_clobber_other_keys(self):
        store.save_positions([{"id": "p1"}])
        self.assertEqual(json.loads(self.path.read_text())["timezone"], "UTC")


if __name__ == "__main__":
    unittest.main()
