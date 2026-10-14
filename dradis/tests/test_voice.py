"""
tests/test_voice.py
───────────────────
What a TTS engine is handed. Car Mode left "_" and "-" in, and the speaker said
"trattino basso" and "trattino".

    cd dradis && python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

from voice import for_voice                                        # noqa: E402


class ForVoice(unittest.TestCase):

    def test_no_underscore_or_dash_reaches_the_speaker(self):
        for text in ("Temporali_2: nessun fulmine", "Radar delle 15:55 - pioggia debole",
                     "Vento da nord-ovest", "- prima\n- seconda", "Lancia /rain",
                     "Meteo_mattina — tutto ok", "10-15 km"):
            out = for_voice(text, "it")
            for bad in ("_", " - ", "—", "–", "/"):
                self.assertNotIn(bad, out, (text, out))

    def test_what_it_becomes(self):
        self.assertEqual(for_voice("Temporali_2: nessun fulmine"), "Temporali 2: nessun fulmine.")
        self.assertEqual(for_voice("Radar delle 15:55 - pioggia debole"),
                         "Radar delle 15:55, pioggia debole.")
        self.assertEqual(for_voice("Vento da nord-ovest"), "Vento da nord ovest.")
        self.assertEqual(for_voice("tra 10-15 km"), "tra 10 a 15 chilometri.")
        self.assertEqual(for_voice("between 10-15 km", "en"), "between 10 to 15 kilometres.")

    def test_italian_decimals_use_the_comma(self):
        self.assertEqual(for_voice("27.5 gradi", "it"), "27,5 gradi.")
        self.assertEqual(for_voice("27.5 degrees", "en"), "27.5 degrees.")

    def test_a_negative_number_keeps_its_sign(self):
        self.assertIn("-3", for_voice("Temperatura -3 gradi"))

    def test_car_mode_work_is_kept(self):
        out = for_voice("<b>Domani</b> 21°C 🌤️ <a href='https://x.it'>mappa</a>")
        self.assertEqual(out, "Domani 21 gradi.")

    def test_idempotent(self):
        once = for_voice("Temporali_2 - nord-ovest, 10-15 km")
        self.assertEqual(for_voice(once), once)


if __name__ == "__main__":
    unittest.main()
