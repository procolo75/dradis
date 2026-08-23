"""
tests/test_geocode.py
─────────────────────
Place-name resolution shared by the monitors, the weather tool and the Web UI — `geocode.py`.

    cd dradis && python3 -m unittest discover tests

No network: `_fetch` is swapped for a function returning the records the API actually
returns for these queries.

What is pinned:
  · The most populous match wins. `count=1` let the API's own ranking choose, and for
    "Napoli" that ranking answers with a village in Gambia, 3000 km from the city.
  · An unknown population counts as zero, so the API's order still decides between
    results that all lack one.
  · A trailing country narrows the search — "Napoli, GM" really does mean Gambia —
    matched on the country code or the country name.
  · A country nothing matches is an error, not a silent fallback to another country.
  · Italian is the default language, and it is not cosmetic: the endpoint searches
    localized name tables, so "Napoli" asked in English does not match the Italian
    city at all (it is indexed there as "Naples").
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

try:
    import geocode as mod
except ModuleNotFoundError as e:          # httpx absent outside the add-on image
    raise unittest.SkipTest(str(e))


def _place(name, cc, pop, lat=0.0, lon=0.0, country=""):
    return {"name": name, "country_code": cc, "country": country,
            "population": pop, "latitude": lat, "longitude": lon}


# What the API really answers for "Napoli": the Gambian village first, the city absent
# from the English tables and present in the Italian ones.
_NAPOLI = [
    _place("Napoli", "GM", None, 13.18, -16.18, "Gambia"),
    _place("Napoli", "US", None, 42.20, -78.89, "United States"),
    _place("Napoli-Nola", "IT", 29, 40.88, 14.52, "Italia"),
    _place("Napoli", "IT", 909048, 40.85, 14.27, "Italia"),
]


class GeocodeTest(unittest.TestCase):

    def _run(self, coro, results):
        async def fake_fetch(name, language, timeout):
            self.asked = (name, language, timeout)
            return results
        original, mod._fetch = mod._fetch, fake_fetch
        try:
            return asyncio.run(coro())
        finally:
            mod._fetch = original

    # ── choosing among the matches ───────────────────────────────────────────

    def test_the_city_wins_over_the_village_in_gambia(self):
        lat, lon, name = self._run(lambda: mod.geocode("Napoli"), _NAPOLI)
        self.assertEqual((round(lat, 2), round(lon, 2)), (40.85, 14.27))
        self.assertEqual(name, "Napoli")

    def test_unknown_population_counts_as_zero(self):
        # Nothing has a population: the API's own order is left in charge.
        results = [_place("Alpha", "FR", None), _place("Beta", "FR", None)]
        _, _, name = self._run(lambda: mod.geocode("Alpha"), results)
        self.assertEqual(name, "Alpha")

    def test_a_populated_result_beats_an_unknown_one(self):
        results = [_place("Alpha", "FR", None), _place("Beta", "FR", 5000)]
        _, _, name = self._run(lambda: mod.geocode("Alpha"), results)
        self.assertEqual(name, "Beta")

    # ── country filter ───────────────────────────────────────────────────────

    def test_a_country_code_narrows_the_search(self):
        lat, _, _ = self._run(lambda: mod.geocode("Napoli, GM"), _NAPOLI)
        self.assertEqual(round(lat, 2), 13.18)

    def test_the_country_name_works_too(self):
        lat, _, _ = self._run(lambda: mod.geocode("Napoli, Gambia"), _NAPOLI)
        self.assertEqual(round(lat, 2), 13.18)

    def test_the_country_is_matched_case_insensitively(self):
        lat, _, _ = self._run(lambda: mod.geocode("Napoli, it"), _NAPOLI)
        self.assertEqual(round(lat, 2), 40.85)

    def test_the_city_alone_is_sent_to_the_api(self):
        self._run(lambda: mod.geocode("Napoli, IT"), _NAPOLI)
        self.assertEqual(self.asked[0], "Napoli")

    def test_a_country_nothing_matches_is_an_error(self):
        with self.assertRaises(ValueError):
            self._run(lambda: mod.geocode("Napoli, JP"), _NAPOLI)

    # ── nothing found ────────────────────────────────────────────────────────

    def test_no_result_raises(self):
        with self.assertRaises(ValueError):
            self._run(lambda: mod.geocode("Nowhere"), [])

    def test_an_empty_query_raises_without_calling_the_api(self):
        self.asked = None
        with self.assertRaises(ValueError):
            self._run(lambda: mod.geocode("  "), _NAPOLI)
        self.assertIsNone(self.asked)

    # ── language ─────────────────────────────────────────────────────────────

    def test_italian_is_the_default_language(self):
        # Not cosmetic: the English tables index the city as "Naples", so a search for
        # "Napoli" in English cannot return it at all.
        self._run(lambda: mod.geocode("Napoli"), _NAPOLI)
        self.assertEqual(self.asked[1], "it")

    def test_search_returns_the_whole_record_for_the_ui(self):
        r = self._run(lambda: mod.search("Napoli"), _NAPOLI)
        self.assertEqual(r["country_code"], "IT")
        self.assertEqual(r["country"], "Italia")


if __name__ == "__main__":
    unittest.main()
