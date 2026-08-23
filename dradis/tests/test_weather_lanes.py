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

The axis and the forecast window are pinned here too:
  · Tick labels carry the LOCAL time of the data. The timestamps are tz-aware and
    matplotlib places those in UTC, so a locator without a tz labelled the whole
    axis one or two hours off the values printed under it.
  · Midnight lines land exactly on the 00:00 tick.
  · The x-axis is pinned to the data. Neither text nor quiver feeds the autoscaler,
    so the limits used to come from the midnight lines and everything past the last
    one was clipped away — eight hours of a 3-day chart starting at midday.
  · The window starts at the run, not at midnight — Open-Meteo always answers from
    00:00 of the current day — and is anchored to the 0/3/6/9 grid so the labels of
    a lane chart stay on round hours.
  · Clipping cuts the timestamps and every series with the same indices. Cutting
    only the timestamps would slide every value onto the wrong hour.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dradis"))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from zoneinfo import ZoneInfo
    from monitors.weather_chart import (
        _COLORS, _clip_window, _plot_value_lanes, _window_start,
    )
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


_ROME = ZoneInfo("Europe/Rome")


class WindowTest(unittest.TestCase):
    """The forecast window starts at the run, anchored to the 3-hour grid."""

    def test_start_is_pushed_to_the_next_multiple_of_three(self):
        for now, expected in [
            (datetime(2026, 8, 23, 10, 16), datetime(2026, 8, 23, 12, 0)),
            (datetime(2026, 8, 23, 11, 59), datetime(2026, 8, 23, 12, 0)),
            (datetime(2026, 8, 23, 12, 0),  datetime(2026, 8, 23, 15, 0)),
            (datetime(2026, 8, 23, 0, 5),   datetime(2026, 8, 23, 3, 0)),
        ]:
            with self.subTest(now=now):
                self.assertEqual(_window_start(now), expected)

    def test_start_rolls_over_midnight(self):
        self.assertEqual(_window_start(datetime(2026, 8, 23, 23, 30)),
                         datetime(2026, 8, 24, 0, 0))

    def test_past_hours_are_dropped(self):
        times = _hours(24)
        hourly = {"v": list(range(24))}
        start = datetime(2026, 8, 23, 12, 0)
        kept, clipped = _clip_window(times, hourly, start, start + timedelta(days=1))
        self.assertEqual(kept[0], start)
        self.assertEqual(clipped["v"][0], 12)

    def test_every_series_is_cut_with_the_same_indices(self):
        # Cutting only the timestamps would print the 00:00 value under the 12:00 label.
        times = _hours(24)
        hourly = {"a": list(range(24)), "b": [x * 10 for x in range(24)]}
        start = datetime(2026, 8, 23, 6, 0)
        kept, clipped = _clip_window(times, hourly, start, start + timedelta(hours=3))
        self.assertEqual([f"{t:%H}" for t in kept], ["06", "07", "08"])
        self.assertEqual(clipped["a"], [6, 7, 8])
        self.assertEqual(clipped["b"], [60, 70, 80])

    def test_the_time_key_is_not_carried_into_the_clipped_series(self):
        times = _hours(6)
        hourly = {"time": ["x"] * 6, "v": list(range(6))}
        _, clipped = _clip_window(times, hourly, times[0], times[3])
        self.assertNotIn("time", clipped)

    def test_a_short_series_does_not_raise(self):
        # ICON EU stops before the window ends; its series is shorter than the mask.
        times = _hours(12)
        kept, clipped = _clip_window(times, {"v": [1, 2, 3]}, times[0], times[-1])
        self.assertEqual(clipped["v"], [1, 2, 3])
        self.assertEqual(len(kept), 11)


class AxisTest(unittest.TestCase):
    """Tick labels and midnight lines follow the data, not UTC."""

    def test_tick_labels_carry_local_time(self):
        # Europe/Rome in August is UTC+2: this is exactly where the axis used to lie.
        times = [(datetime(2026, 8, 23, 0, 0) + timedelta(hours=i)).replace(tzinfo=_ROME)
                 for i in range(72)]
        fig, ax = plt.subplots()
        try:
            ax.plot(times, range(72))
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=_ROME))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%H:%M", tz=_ROME))
            fig.canvas.draw()
            for loc, lab in zip(ax.get_xticks(), ax.get_xticklabels()):
                local = mdates.num2date(loc).astimezone(_ROME)
                self.assertEqual(lab.get_text(), f"{local:%d/%m}\n{local:%H:%M}")
        finally:
            plt.close(fig)

    def test_midnight_line_sits_on_the_midnight_tick(self):
        midnight = datetime(2026, 8, 24, 0, 0, tzinfo=_ROME)
        fig, ax = plt.subplots()
        try:
            ax.plot([midnight - timedelta(hours=12), midnight + timedelta(hours=12)], [0, 1])
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=_ROME))
            fig.canvas.draw()
            ticks = list(ax.get_xticks())
            self.assertIn(True, [abs(mdates.date2num(midnight) - t) < 1e-9 for t in ticks])
        finally:
            plt.close(fig)


    def test_axis_covers_every_hour_of_data(self):
        # The limits used to be inherited from the midnight lines, dropping the hours
        # after the last one. A chart starting at midday lost its final two labels.
        times = [(datetime(2026, 8, 23, 12, 0) + timedelta(hours=i)).replace(tzinfo=_ROME)
                 for i in range(72)]
        model_data = {"ecmwf_ifs04": (times, {"v": list(range(72))})}
        fig, ax = plt.subplots()
        try:
            _plot_value_lanes(ax, "v", ["ecmwf_ifs04"], model_data, _NOTE)
            for t in times:                    # drawn after, as the chart does
                if t.hour in (0, 12):
                    ax.axvline(t)
            lo, hi = ax.get_xlim()
            outside = [t for t in times if not (lo <= mdates.date2num(t) <= hi)]
            self.assertEqual(outside, [])
        finally:
            plt.close(fig)

if __name__ == "__main__":
    unittest.main()
