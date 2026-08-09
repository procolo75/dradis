"""
live_monitors/replay.py
────────────────────────
Offline replay of a recorded storm against the lightning decision core.

Enable `record_strikes` on a lightning monitor and it writes every strike it sees
to /data/lightning_rec/<monitor_id>-<date>.ndjson. This tool plays that file back
through the exact same perception and decision code the live monitor runs, so
thresholds can be tuned on real storms instead of by waiting for the next one.

Usage (inside the add-on container, where the code lives in /app/dradis):

    cd /app/dradis
    python3 -m live_monitors.replay /data/lightning_rec/abc-2026-08-09.ndjson --monitor abc
    python3 -m live_monitors.replay <file> --lat 40.85 --lon 14.27 --radius 100
    python3 -m live_monitors.replay <file> --monitor abc --compare

From a checkout, run it from the `dradis/` directory with the package prefixed:

    python3 -m dradis.live_monitors.replay <file> --lat 40.85 --lon 14.27

`--compare` runs all three sensitivity presets over the same recording and prints
their alert timelines side by side — the intended way to pick a preset.
"""

import argparse
import json
import sys
from datetime import datetime

from .lightning_core import (
    LEVEL_NAMES, PRESETS, DEFAULT_SENSITIVITY,
    ObservableTracker, ThreatStateMachine, get_preset,
    WINDOW_MIN, POLL_INTERVAL_STATIC,
)

LIVE_MONITORS_PATH = "/data/live_monitors.json"


def load_strikes(path: str) -> list:
    """Read an NDJSON recording into a time-sorted [(t, lat, lon)] list."""
    strikes = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                strikes.append((float(rec["t"]), float(rec["lat"]), float(rec["lon"])))
            except (ValueError, KeyError, TypeError):
                bad += 1
    if bad:
        print(f"# skipped {bad} unparsable lines", file=sys.stderr)
    strikes.sort(key=lambda s: s[0])
    return strikes


def load_monitor(monitor_id: str) -> dict:
    with open(LIVE_MONITORS_PATH, "r", encoding="utf-8") as fh:
        monitors = json.load(fh)
    for m in monitors:
        if m.get("id") == monitor_id:
            return m
    raise SystemExit(f"monitor '{monitor_id}' not found in {LIVE_MONITORS_PATH}")


def replay(strikes: list, origin: tuple, radius_km: float, sensitivity: str,
           poll_sec: float, verbose: bool) -> list:
    """Run the pipeline over the recording. Returns the list of alert events."""
    tracker = ObservableTracker()
    machine = ThreatStateMachine(get_preset(sensitivity))
    events = []

    start, end = strikes[0][0], strikes[-1][0]
    cursor = 0
    buffer: list = []
    now = start

    while now <= end + poll_sec:
        while cursor < len(strikes) and strikes[cursor][0] <= now:
            buffer.append(strikes[cursor])
            cursor += 1
        buffer = [s for s in buffer if s[0] >= now - WINDOW_MIN * 60]

        obs = tracker.observe(buffer, origin, now, radius_km)
        decision = machine.evaluate(obs, now)
        if decision is not None:
            machine.commit(decision, now)          # replay assumes delivery works
            events.append((now, decision, obs))

        if verbose:
            print(f"{_clock(now)}  {LEVEL_NAMES[machine.level]:<7} "
                  f"d10={_n(obs.d10)}/{_n(obs.d10_s)} "
                  f"R={obs.r_near:5.2f} vc={_n(obs.v_c_s, '{:+.1f}')} "
                  f"eta={_n(obs.eta_min, '{:.0f}')} n={obs.strikes_total}"
                  + (f"   <<< {LEVEL_NAMES[decision.level]}"
                     f"{' (periodic)' if decision.periodic else ''}" if decision else ""))
        now += poll_sec

    return events


def _n(value, fmt="{:.1f}") -> str:
    return "  — " if value is None else fmt.format(value)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _summarise(name: str, events: list) -> None:
    print(f"\n── {name} ─────────────────────────────────────────")
    if not events:
        print("   no alerts")
        return
    for ts, decision, obs in events:
        tag = "periodic" if decision.periodic else "LEVEL"
        print(f"   {_clock(ts)}  {tag:<8} {LEVEL_NAMES[decision.level]:<7} "
              f"d10={_n(obs.d10_s)} km  R={obs.r_near:.2f}/min  "
              f"vc={_n(obs.v_c_s, '{:+.1f}')} km/h  eta={_n(obs.eta_min, '{:.0f}')}")
    changes = [e for e in events if not e[1].periodic]
    print(f"   → {len(changes)} level changes, {len(events) - len(changes)} re-alerts")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", help="NDJSON file written by record_strikes")
    ap.add_argument("--monitor", help="read lat/lon/radius/sensitivity from live_monitors.json")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--radius", type=float, default=100.0)
    ap.add_argument("--sensitivity", default=None, choices=sorted(PRESETS))
    ap.add_argument("--poll", type=float, default=POLL_INTERVAL_STATIC,
                    help="poll interval in seconds (default: %(default)s)")
    ap.add_argument("--compare", action="store_true",
                    help="replay every sensitivity preset over the same recording")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print every poll, not just the alerts")
    args = ap.parse_args(argv)

    lat, lon, radius = args.lat, args.lon, args.radius
    sensitivity = args.sensitivity
    if args.monitor:
        cfg = load_monitor(args.monitor)
        lat = lat if lat is not None else float(cfg.get("latitude", 0))
        lon = lon if lon is not None else float(cfg.get("longitude", 0))
        radius = float(cfg.get("radius_km", radius))
        sensitivity = sensitivity or cfg.get("sensitivity")
    if lat is None or lon is None:
        ap.error("provide --lat/--lon, or --monitor to read them from live_monitors.json")
    sensitivity = sensitivity or DEFAULT_SENSITIVITY

    strikes = load_strikes(args.recording)
    if not strikes:
        print("no strikes in recording", file=sys.stderr)
        return 1

    span_min = (strikes[-1][0] - strikes[0][0]) / 60.0
    print(f"{len(strikes)} strikes over {span_min:.0f} min "
          f"({_clock(strikes[0][0])} → {_clock(strikes[-1][0])})  "
          f"origin {lat:.4f},{lon:.4f}  radius {radius:.0f} km  poll {args.poll:.0f}s")

    presets = sorted(PRESETS) if args.compare else [sensitivity]
    for name in presets:
        if args.verbose:
            print(f"\n=== preset: {name} ===")
        events = replay(strikes, (lat, lon), radius, name, args.poll, args.verbose)
        _summarise(f"preset: {name}", events)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
