"""
tests/test_weather_lanes.py
───────────────────────────
The numeric lane renderer of the weather chart monitor — `monitors/weather_chart.py`.

    cd dradis && python3 -m unittest discover tests

No network: every test hands `_plot_value_lanes` the `{model_id: (times, hourly)}`
structure the runner builds, and reads back what was drawn on the axes.

What is pinned:
  · Labels are drawn every 3 hours, one lane per model.
  · Rainfall is SUMMED over each 3-hour window, not sampled. Sampling one hour in
    three would silently drop two thirds of the rain — a shower at 13:00 vanishes
    between the 12:00 and 15:00 readings.
  · Gusts take the window PEAK. A gust chart exists to show the maximum.
  · Variables without an `aggregate` are sampled, not aggregated — cloud cover is
    a state, and summing three hours of it would produce 300.
  · Zero prints as a bare "0" (never "0.0") and is dimmed, so the few real values
    in a mostly dry lane are the ones that stand out.
  · A model that does not carry the variable is dropped, not drawn as an empty
    lane — ICON EU has no precipitation probability, and a labelled empty row
    reads as "no rain expected" rather than "no data".
  · Colour follows the model's position in the selection, not the lane index, so
    dropping a lane does not recolour the models below it.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from monitors.weather_chart import _COLORS, _plot_value_lanes
except ModuleNotFoundError as e:          # matplotlib absent outside the add-on image
    raise unittest.SkipTest(str(e))


_START = datetime(2026, 8, 23, 0, 0)
_NOTE = "note"


def _hours(n: int) -> list[datetime]:
    return [_START + timedelta(hours=i) for i in range(n)]


def _data(**series: list) -> dict:
    """{model_id: (times, hourly)} for one variable named "v"."""
    return {m: (_hours(len(vals)), {"v": vals}) for m, vals in series.items()}


class LaneRenderingTest(unittest.TestCase):

    def setUp(self):
        self.fig, self.ax = plt.subplots()

    def tearDown(self):
        plt.close(self.fig)

    def _draw(self, model_data, **kwargs):
        _plot_value_lanes(self.ax, "v", list(model_data.keys()), model_data, _NOTE, **kwargs)
        return [t for t in self.ax.texts if t.get_text() != _NOTE]

    def _texts(self, model_data, **kwargs):
        return [t.get_text() for t in self._draw(model_data, **kwargs)]

    # ── cadence ──────────────────────────────────────────────────────────────

    def test_labels_every_three_hours(self):
        texts = self._texts(_data(ecmwf_ifs04=list(range(1, 13))))
        self.assertEqual(texts, ["1", "4", "7", "10"])

    def test_one_lane_per_model(self):
        data = _data(ecmwf_ifs04=[10] * 6, gfs025=[20] * 6)
        self._draw(data)
        self.assertEqual(
            [t.get_text() for t in self.ax.get_yticklabels()],
            ["ECMWF IFS 9km", "GFS Global"],
        )

    # ── aggregation ──────────────────────────────────────────────────────────

    def test_rainfall_is_summed_over_the_window(self):
        # A shower falling entirely at 13:00 must survive: sampling loses it.
        rain = [0.0] * 12
        rain[1] = 0.4
        rain[4] = 2.0
        texts = self._texts(_data(ecmwf_ifs04=rain), decimals=1, aggregate="sum")
        self.assertEqual(texts, ["0.4", "2.0", "0", "0"])

    def test_gusts_take_the_window_peak(self):
        texts = self._texts(_data(ecmwf_ifs04=[5, 38, 7, 9, 11, 10]), aggregate="max")
        self.assertEqual(texts, ["38", "11"])

    def test_state_variables_are_sampled_not_aggregated(self):
        # Cloud cover summed over three hours would read 300.
        texts = self._texts(_data(ecmwf_ifs04=[100] * 6))
        self.assertEqual(texts, ["100", "100"])

    def test_partial_window_is_aggregated_from_what_exists(self):
        texts = self._texts(_data(ecmwf_ifs04=[1.0, 2.0, 3.0, 4.0]),
                            decimals=1, aggregate="sum")
        self.assertEqual(texts, ["6.0", "4.0"])

    def test_none_values_are_skipped(self):
        texts = self._texts(_data(ecmwf_ifs04=[None, None, None, 5, 6, 7]))
        self.assertEqual(texts, ["5"])

    def test_a_window_of_only_none_draws_nothing(self):
        texts = self._texts(_data(ecmwf_ifs04=[None, None, None, 1.0, 1.0, 1.0]),
                            decimals=1, aggregate="sum")
        self.assertEqual(texts, ["3.0"])

    # ── zeros ────────────────────────────────────────────────────────────────

    def test_zero_prints_bare_and_dimmed(self):
        drawn = self._draw(_data(ecmwf_ifs04=[0.0, 0.0, 0.0, 1.5, 0.0, 0.0]),
                           decimals=1, aggregate="sum")
        self.assertEqual([t.get_text() for t in drawn], ["0", "1.5"])
        self.assertEqual(drawn[0].get_color(), "#555555")
        self.assertEqual(drawn[1].get_color(), _COLORS[0])

    # ── missing models ───────────────────────────────────────────────────────

    def test_model_without_the_variable_is_dropped(self):
        data = {
            "ecmwf_ifs04": (_hours(6), {"v": [1] * 6}),
            "icon_eu":     (_hours(6), {}),            # excludes this variable
            "gfs025":      (_hours(6), {"v": [3] * 6}),
        }
        self._draw(data)
        self.assertEqual(
            [t.get_text() for t in self.ax.get_yticklabels()],
            ["ECMWF IFS 9km", "GFS Global"],
        )

    def test_colour_follows_the_model_not_the_lane(self):
        # GFS is third in the selection and keeps the third colour even though
        # the lane above it was dropped.
        data = {
            "ecmwf_ifs04": (_hours(3), {"v": [1, 1, 1]}),
            "icon_eu":     (_hours(3), {}),
            "gfs025":      (_hours(3), {"v": [3, 3, 3]}),
        }
        drawn = self._draw(data)
        self.assertEqual([t.get_color() for t in drawn], [_COLORS[0], _COLORS[2]])


if __name__ == "__main__":
    unittest.main()
