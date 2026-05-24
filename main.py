"""Marine Route Optimizer — entry point.

Default behavior: launch the Tk GUI. The previous one-shot console mode is
still available via ``--cli`` for scripted use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from optimizer import (
    DEMO_WAYPOINTS,
    estimate_duration_hours,
    format_duration,
    load_waypoints,
    optimize,
)


def run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="marine-route-optimizer", description="Marine Route Optimizer (CLI mode)")
    parser.add_argument("input", nargs="?", help="CSV or JSON file with waypoints (name,lat,lon)")
    parser.add_argument("--loop", action="store_true", help="Return to the starting waypoint")
    parser.add_argument("--demo", action="store_true", help="Run with a built-in demo dataset")
    parser.add_argument("--speed", type=float, default=15.0, help="Cruising speed in knots (default: 15)")
    args = parser.parse_args(argv)

    if args.demo or not args.input:
        waypoints = DEMO_WAYPOINTS
        source = "built-in demo dataset"
    else:
        path = Path(args.input)
        if not path.exists():
            print(f"error: input file not found: {path}", file=sys.stderr)
            return 2
        waypoints = load_waypoints(path)
        source = str(path)

    if len(waypoints) < 2:
        print("error: need at least two waypoints", file=sys.stderr)
        return 2

    route, total = optimize(waypoints, return_to_start=args.loop)
    eta_h = estimate_duration_hours(total, args.speed)

    print(f"Input: {source} ({len(waypoints)} waypoints)\n")
    print("Optimized route:")
    for i, wp in enumerate(route, start=1):
        print(f"  {i:>3}. {wp.name:<24} ({wp.lat:+.4f}, {wp.lon:+.4f})")
    print()
    print(f"Total distance: {total:,.1f} NM ({total * 1.852:,.1f} km)")
    print(f"At {args.speed:g} kn: {format_duration(eta_h)}")
    return 0


def run_gui() -> int:
    from gui import launch  # imported lazily so --cli doesn't require Tk

    launch()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--cli":
        return run_cli(argv[1:])
    if argv and argv[0] in {"-h", "--help"}:
        print("Usage:")
        print("  marine-route-optimizer              # launch GUI")
        print("  marine-route-optimizer --cli [...]  # one-shot CLI; pass --help for options")
        return 0
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
