"""
tests/test_campania_alert.py
────────────────────────────
The Civil Protection alert monitor for Campania — `monitors/campania_alert.py`.

    cd dradis && python3 -m unittest discover tests

No network: every test feeds the runner bulletins shaped like the ones the Centro
Funzionale REST API actually returns.

What is pinned:
  · BOTH days, always. The monitor answers "is there an alert", and one day is
    half an answer — tomorrow is also the actionable half, since today's window
    is already running by the time anyone reads it. There is no day switch.
  · The threshold spans both days. An orange tomorrow fires a monitor set to
    orange even when today is entirely green, and vice versa.
  · Silence is the default. Both days green, or a bulletin not published yet,
    produce the empty string — the scheduler sends nothing on an empty result,
    so a regression here turns a quiet monitor into a daily "all clear".
  · Tomorrow failing must not cost today's alert. Its fetch error is reported
    inside the message; today's failure is the monitor failing and must raise.
  · The zone NAMES are carried in the module. The API returns only the number,
    and the names live nowhere but the site's JavaScript bundle, so nothing else
    can catch them drifting.
  · `fenomeni` / `scenari` are printed once per day. The bulletin repeats the
    identical paragraph on every zone in alert; eight copies is the whole message.
  · Timestamps come out as "DD/MM/YYYY HH:MM" and the notice number carries no
    slash. Car Mode reads a slash between digits as a ratio, so "72/2026" is
    spoken as "72 su 2026" and "11:00 - 21/08/2026" loses its date.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

try:
    from monitors.campania_alert import (
        ZONES, _dedup, _max_level, _stamp, run_campania_alert_monitor,
    )
except ModuleNotFoundError as e:          # httpx absent outside the add-on image
    raise unittest.SkipTest(str(e))


_PHENOMENA = "Isolati rovesci e temporali, puntualmente di moderata intensità."
_SCENARIOS = "- Possibili allagamenti di locali interrati e di quelli a pian terreno."


def _zone(num: int, level: int, risks: str = "Idrogeologico per temporali") -> dict:
    if level <= 1:
        return {"zona": num, "livello": 1, "tipoRischi": "",
                "fenomeni": "", "scenari": ""}
    return {"zona": num, "livello": level, "tipoRischi": risks,
            "fenomeni": _PHENOMENA, "scenari": _SCENARIOS}


def _bulletin(levels: dict[int, int], *, notice: int = 72, day: str = "21/08/2026") -> dict:
    return {
        "idBollettino": 702,
        "dataEmissione": f"11:00 - {day}",
        "dataDa": f"14:00 - {day}",
        "dataA": "14:00 - 22/08/2026",
        "numeroAvviso": notice,
        "anno": 2026,
        "firmaBollettino": "Andrea Monda",
        "bollettinoMeteoBindList": [_zone(n, levels.get(n, 1)) for n in range(1, 9)],
    }


def _no_zones(*, issued: bool, day: str = "22/08/2026") -> dict:
    """What the alert endpoints answer when the window lists no zone at all.

    `checkAvviso` is the entire difference between "nessuna allerta" and "not
    decided yet", and only tomorrow's endpoint ever answers False.
    """
    return {"bollettinoMeteoBindList": [], "idBollettino": None, "numeroAvviso": None,
            "dataEmissione": None, "dataDa": f"14:00 - {day}",
            "dataA": "14:00 - 23/08/2026", "firmaBollettino": None,
            "checkAvviso": issued}


def _unpublished() -> dict:
    """What findAllertaDomaniNew answers for most of every morning."""
    return _no_zones(issued=False)


def _run(monitor: dict, today, tomorrow) -> str:
    """Drive the real runner with both fetches replaced by fixed bulletins."""
    import monitors.campania_alert as mod

    async def _fake_fetch(day):
        answer = today if day == "today" else tomorrow
        if isinstance(answer, BaseException):
            raise answer
        return answer

    original, mod._fetch_bulletin = mod._fetch_bulletin, _fake_fetch
    try:
        return asyncio.run(run_campania_alert_monitor(monitor, tz_name="Europe/Rome"))
    finally:
        mod._fetch_bulletin = original


class BothDaysTest(unittest.TestCase):

    def test_both_days_are_always_reported(self):
        out = _run({"min_level": 2}, _bulletin({1: 2}), _bulletin({4: 2}))
        self.assertIn("OGGI", out)
        self.assertIn("DOMANI", out)
        self.assertLess(out.index("OGGI"), out.index("DOMANI"))

    def test_a_green_today_still_shows_up_next_to_an_alerted_tomorrow(self):
        out = _run({"min_level": 2}, _bulletin({}), _bulletin({4: 3}))
        self.assertIn("Nessuna allerta su tutte le zone", out)
        self.assertIn("🟠 ARANCIONE", out)

    def test_an_empty_list_on_a_decided_day_is_green_not_unpublished(self):
        # The regression of v4.7.3: the region publishes an alert bulletin only
        # when there is an alert, so a quiet day comes back with no zones at all.
        # Reading that as "non ancora emesso" turned every quiet day into a
        # report that the region had said nothing, which is not what it said.
        out = _run({"min_level": 1}, _no_zones(issued=True), _no_zones(issued=True))
        self.assertEqual(out.count("Nessuna allerta su tutte le zone"), 2)
        self.assertNotIn("non ancora emesso", out)

    def test_today_is_green_on_an_empty_list_whatever_the_flag_says(self):
        # Today's window is already running: the site paints its map green with
        # no zones listed, and never grey.
        out = _run({"min_level": 1}, _no_zones(issued=False), _bulletin({}))
        self.assertIn("Nessuna allerta su tutte le zone", out.split("DOMANI")[0])
        self.assertNotIn("non ancora emesso", out)

    def test_an_unpublished_tomorrow_is_said_so_not_hidden(self):
        out = _run({"min_level": 2}, _bulletin({1: 2}), _unpublished())
        self.assertIn("non ancora emesso", out)
        # Its window is known even before it is issued, so it is still shown.
        self.assertIn("22/08/2026 14:00", out)

    def test_each_day_keeps_its_own_notice_number(self):
        out = _run({"min_level": 2},
                   _bulletin({1: 2}),
                   _bulletin({1: 2}, notice=73, day="22/08/2026"))
        self.assertIn("Avviso n. 72 del 2026", out)
        self.assertIn("Avviso n. 73 del 2026", out)


class SilenceTest(unittest.TestCase):

    def test_both_days_green_says_nothing(self):
        self.assertEqual(_run({}, _bulletin({}), _bulletin({})), "")

    def test_green_today_and_unpublished_tomorrow_says_nothing(self):
        self.assertEqual(_run({}, _bulletin({}), _unpublished()), "")

    def test_below_threshold_on_both_days_says_nothing(self):
        self.assertEqual(_run({"min_level": 3}, _bulletin({1: 2}), _bulletin({4: 2})), "")

    def test_level_one_monitor_reports_an_all_green_day(self):
        # The opt-in for someone who wants the daily confirmation.
        out = _run({"min_level": 1}, _bulletin({}), _bulletin({}))
        self.assertEqual(out.count("Nessuna allerta su tutte le zone"), 2)


class ThresholdTest(unittest.TestCase):

    def test_tomorrow_alone_can_fire_the_monitor(self):
        out = _run({"min_level": 3}, _bulletin({}), _bulletin({4: 3}))
        self.assertIn("Zona 4", out)

    def test_today_alone_can_fire_the_monitor(self):
        out = _run({"min_level": 3}, _bulletin({4: 3}), _unpublished())
        self.assertIn("Zona 4", out)

    def test_threshold_is_inclusive(self):
        self.assertTrue(_run({"min_level": 3}, _bulletin({4: 3}), _bulletin({})))

    def test_a_single_zone_anywhere_fires_the_monitor(self):
        # The threshold reads the region, not one zone: zone 8 is at the far end
        # of Campania from zone 1 and must still trigger.
        out = _run({"min_level": 2}, _bulletin({8: 2}), _unpublished())
        self.assertIn("Zona 8", out)
        self.assertIn("Verdi: 1, 2, 3, 4, 5, 6, 7", out)

    def test_out_of_range_min_level_is_clamped(self):
        self.assertTrue(_run({"min_level": 0}, _bulletin({}), _bulletin({})))
        self.assertEqual(_run({"min_level": 9}, _bulletin({1: 3}), _bulletin({1: 3})), "")


class FetchFailureTest(unittest.TestCase):

    def test_a_failing_tomorrow_does_not_cost_todays_alert(self):
        out = _run({"min_level": 2}, _bulletin({1: 4}), RuntimeError("HTTP 503"))
        self.assertIn("🔴 ROSSO", out)
        self.assertIn("non raggiungibile", out)
        self.assertIn("HTTP 503", out)

    def test_a_failing_today_raises(self):
        # The scheduler turns this into a visible "monitor failed" message. A
        # silent green would be the one outcome nobody could notice.
        with self.assertRaises(RuntimeError):
            _run({"min_level": 2}, RuntimeError("HTTP 500"), _bulletin({}))

    def test_a_failing_tomorrow_alone_is_not_enough_to_speak(self):
        # Today green, tomorrow unreachable: nothing is known to be wrong, so the
        # monitor stays quiet rather than reporting an error as an alert.
        self.assertEqual(_run({}, _bulletin({}), RuntimeError("boom")), "")


class ReportTest(unittest.TestCase):

    def setUp(self):
        self.out = _run({"min_level": 2}, _bulletin({1: 2, 3: 3, 5: 2}), _unpublished())

    def test_worst_zone_comes_first(self):
        self.assertLess(self.out.index("Zona 3"), self.out.index("Zona 1"))

    def test_zone_names_are_spelled_out(self):
        self.assertIn(ZONES[1], self.out)
        self.assertIn(ZONES[3], self.out)

    def test_green_zones_are_one_line(self):
        self.assertIn("🟢 Verdi: 2, 4, 6, 7, 8", self.out)
        for num in (2, 4, 6, 7, 8):
            self.assertNotIn(f"Zona {num}", self.out)

    def test_repeated_paragraphs_are_printed_once(self):
        self.assertEqual(self.out.count(_PHENOMENA), 1)
        self.assertEqual(self.out.count(_SCENARIOS), 1)

    def test_notice_number_carries_no_slash(self):
        self.assertIn("Avviso n. 72 del 2026", self.out)

    def test_timestamps_lead_with_the_date(self):
        self.assertIn("emesso 21/08/2026 11:00", self.out)
        self.assertIn("dal 21/08/2026 14:00 al 22/08/2026 14:00", self.out)

    def test_scenarios_are_truncated_per_day(self):
        today = _bulletin({1: 2})
        today["bollettinoMeteoBindList"][0]["scenari"] = "x" * 5000
        out = _run({"min_level": 2}, today, _unpublished())
        self.assertIn("…", out)
        self.assertNotIn("x" * 2000, out)

    def test_english_keeps_the_regions_own_wording(self):
        # Level names and headings translate; the bulletin's prose does not —
        # there is no LLM in this path to translate it, and inventing one would
        # be worse than leaving the official text alone.
        out = _run({"min_level": 2, "language": "en"}, _bulletin({1: 2}), _unpublished())
        self.assertIn("🟡 YELLOW", out)
        self.assertIn("TODAY", out)
        self.assertIn("TOMORROW", out)
        self.assertIn("Expected phenomena", out)
        self.assertIn(_PHENOMENA, out)


class EndpointTest(unittest.TestCase):
    """The two endpoints the site's own home-page map calls, and no others.

    The `findLastBollettino` pair used until v4.7.4 answers a different question:
    it leaves `dataDa` null on today's window, which the message printed as
    "dal ? al ...".
    """

    def test_the_alert_endpoints_are_the_ones_asked(self):
        import monitors.campania_alert as mod
        self.assertTrue(mod._ENDPOINTS["today"].endswith("/findLastAllertaNew"))
        self.assertTrue(mod._ENDPOINTS["tomorrow"].endswith("/findAllertaDomaniNew"))


class DetailFallbackTest(unittest.TestCase):
    """`fenomeni` / `scenari` arrive inline today; the monitor survives if they stop.

    The alert endpoints feed a map that needs only zona and livello, so nothing
    on the site would break if their prose went away — this monitor would lose
    half its message. The detail endpoint is asked only when it has to be.
    """

    def _detail(self, data, detail):
        import monitors.campania_alert as mod
        calls = []

        async def _fake_get(url):
            calls.append(url)
            if isinstance(detail, BaseException):
                raise detail
            return detail

        original, mod._get_json = mod._get_json, _fake_get
        try:
            return asyncio.run(mod._with_detail(data)), calls
        finally:
            mod._get_json = original

    def test_prose_already_inline_costs_no_second_request(self):
        out, calls = self._detail(_bulletin({1: 2}), None)
        self.assertEqual(calls, [])
        self.assertEqual(out["bollettinoMeteoBindList"][0]["fenomeni"], _PHENOMENA)

    def test_an_all_green_day_costs_no_second_request(self):
        _, calls = self._detail(_bulletin({}), None)
        self.assertEqual(calls, [])

    def test_a_stripped_alert_is_filled_in_from_the_detail_endpoint(self):
        stripped = _bulletin({1: 2})
        for zone in stripped["bollettinoMeteoBindList"]:
            zone["fenomeni"] = zone["scenari"] = None
        out, calls = self._detail(stripped, _bulletin({1: 2}))
        self.assertEqual(len(calls), 1)
        self.assertIn("/findByIdBollettino/702", calls[0])
        self.assertEqual(out["bollettinoMeteoBindList"][0]["fenomeni"], _PHENOMENA)
        # The wrapper stays the alert endpoint's: only the zones are replaced.
        self.assertEqual(out["dataDa"], stripped["dataDa"])

    def test_a_failing_detail_endpoint_keeps_the_levels(self):
        stripped = _bulletin({1: 4})
        for zone in stripped["bollettinoMeteoBindList"]:
            zone["fenomeni"] = zone["scenari"] = None
        out, _ = self._detail(stripped, RuntimeError("HTTP 500"))
        self.assertEqual(_max_level(out), 4)


class HelpersTest(unittest.TestCase):

    def test_stamp_swaps_clock_and_date(self):
        self.assertEqual(_stamp("11:00 - 21/08/2026"), "21/08/2026 11:00")

    def test_stamp_leaves_anything_else_alone(self):
        for raw in ("21/08/2026", "", None, "aggiornamento - straordinario"):
            self.assertEqual(_stamp(raw), str(raw or "").strip())

    def test_dedup_keeps_order_and_drops_blanks(self):
        self.assertEqual(_dedup(["b", "", "a", "b", None, " a "]), ["b", "a"])

    def test_max_level_reads_a_bulletin(self):
        self.assertEqual(_max_level(_bulletin({1: 2, 4: 3})), 3)

    def test_max_level_of_nothing_is_green(self):
        for empty in (_unpublished(), RuntimeError("boom"), None):
            self.assertEqual(_max_level(empty), 1)

    def test_only_tomorrow_can_be_undecided(self):
        from monitors.campania_alert import _is_issued
        self.assertTrue(_is_issued(_no_zones(issued=True), "tomorrow"))
        self.assertFalse(_is_issued(_no_zones(issued=False), "tomorrow"))
        for data in (_no_zones(issued=False), RuntimeError("boom"), None):
            self.assertTrue(_is_issued(data, "today"))

    def test_every_zone_number_has_a_name(self):
        self.assertEqual(sorted(ZONES), list(range(1, 9)))


class ResilienceTest(unittest.TestCase):

    def test_a_missing_level_is_read_as_green(self):
        # Defensive, and the right direction to fail: a null level must never be
        # promoted into an alert.
        today = _bulletin({})
        today["bollettinoMeteoBindList"][0]["livello"] = None
        self.assertEqual(_run({}, today, _unpublished()), "")

    def test_a_bulletin_without_a_notice_number_still_reports(self):
        today = {**_bulletin({1: 4}), "numeroAvviso": None}
        out = _run({"min_level": 2}, today, _unpublished())
        self.assertIn("🔴 ROSSO", out)
        self.assertNotIn("Avviso n.", out)

    def test_an_unknown_zone_number_falls_back_to_its_number(self):
        today = _bulletin({})
        today["bollettinoMeteoBindList"].append(_zone(9, 2))
        self.assertIn("Zona 9", _run({"min_level": 2}, today, _unpublished()))


if __name__ == "__main__":
    unittest.main()
