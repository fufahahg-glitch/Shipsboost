"""Marine Route Optimizer — entry point.

Default behavior: launch the Tk GUI. CLI mode (``--cli``) keeps a scriptable
one-shot interface and exposes the new weather/economy/GRIB/export features.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from optimizer import (
    DEMO_WAYPOINTS,
    VesselParams,
    estimate_duration_hours,
    format_duration,
    load_waypoints,
    optimize,
    optimize_route_with_weather,
)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marine-route-optimizer",
                                     description="Marine Route Optimizer (CLI mode)")
    parser.add_argument("input", nargs="?", help="CSV or JSON file with waypoints (name,lat,lon)")
    parser.add_argument("--loop", action="store_true", help="Return to the starting waypoint")
    parser.add_argument("--demo", action="store_true", help="Run with the built-in demo dataset")
    parser.add_argument("--speed", type=float, default=15.0, help="Cruising speed in knots (default: 15)")
    parser.add_argument("--vessel", default="container",
                        help="Vessel type key: container, tanker, bulker, yacht, fishing, cruise, tug, generic")
    parser.add_argument("--length", type=float, default=200.0, help="Vessel waterline length, m (default: 200)")
    parser.add_argument("--draft", type=float, default=10.0, help="Vessel draft, m (default: 10)")

    parser.add_argument("--meteo", action="store_true",
                        help="Fetch live weather from Open-Meteo (falls back to climatology offline)")
    parser.add_argument("--climate", action="store_true",
                        help="Force climatology fallback (no network)")
    parser.add_argument("--grib", metavar="PATH", help="Use a GRIB file as the weather source")
    parser.add_argument("--mode", choices=["time", "fuel", "safety", "economy"], default="economy",
                        help="Optimization objective (default: economy)")
    parser.add_argument("--avoid-shallow", action="store_true",
                        help="Reject segments shallower than draft + clearance")
    parser.add_argument("--min-clearance", type=float, default=5.0,
                        help="Minimum clearance below keel, m (default: 5)")
    parser.add_argument("--forecast-hours", type=int, default=0,
                        help="Offset the forecast start by N hours from now (default: 0)")

    parser.add_argument("--export-gpx", metavar="PATH", help="Write a GPX file after optimization")
    parser.add_argument("--export-nmea", metavar="PATH", help="Write an NMEA-0183 file after optimization")
    parser.add_argument("--export-json", metavar="PATH", help="Write a JSON file after optimization")
    return parser


def _load_weather(args, waypoints, start_dt):
    if args.grib:
        from grib_loader import load_grib_file, samples_from_grib
        info = load_grib_file(args.grib)
        return samples_from_grib(info, waypoints, start_dt), f"GRIB ({len(info.messages)} msg)"
    if args.meteo or args.climate:
        from weather_api import get_weather_for_route
        use_api = bool(args.meteo) and not args.climate
        samples = get_weather_for_route(waypoints, start_dt, args.speed, use_api=use_api)
        return samples, "open-meteo" if use_api else "climate"
    return None, "no weather"


def run_cli(argv: list[str]) -> int:
    args = _build_cli_parser().parse_args(argv)

    if args.demo or not args.input:
        waypoints = list(DEMO_WAYPOINTS)
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

    start_dt = datetime.now(tz=timezone.utc).replace(microsecond=0) + timedelta(hours=args.forecast_hours)
    weather, weather_label = _load_weather(args, waypoints, start_dt)

    print(f"Input:   {source} ({len(waypoints)} waypoints)")
    print(f"Vessel:  {args.vessel}   length {args.length:.0f} m   draft {args.draft:.1f} m   v_max {args.speed:g} kn")
    print(f"Mode:    {args.mode}   weather: {weather_label}")
    print(f"Loop:    {'yes' if args.loop else 'no'}   forecast offset: {args.forecast_hours} h")
    print()

    if weather is None:
        route, total = optimize(waypoints, return_to_start=args.loop)
        eta_h = estimate_duration_hours(total, args.speed)
        print("Optimized order (distance-only):")
        for i, wp in enumerate(route, start=1):
            print(f"  {i:>3}. {wp.name:<24} ({wp.lat:+.4f}, {wp.lon:+.4f})")
        print(f"\nTotal distance: {total:,.1f} NM ({total * 1.852:,.1f} km)")
        print(f"At {args.speed:g} kn: {format_duration(eta_h)}")
        report = None
    else:
        vessel = VesselParams(type_key=args.vessel, v_max_kn=args.speed,
                              length_m=args.length, draft_m=args.draft)
        clearance = args.min_clearance if args.avoid_shallow else None
        report = optimize_route_with_weather(
            waypoints, vessel, weather, mode=args.mode,
            min_clearance_m=clearance, return_to_start=args.loop,
        )
        print("Optimized order:")
        for i, wp in enumerate(report.route, start=1):
            print(f"  {i:>3}. {wp.name:<24} ({wp.lat:+.4f}, {wp.lon:+.4f})")
        print()
        print(f"Distance:    {report.total_distance_nm:,.1f} NM")
        print(f"Time:        {format_duration(report.total_hours)}")
        print(f"Avg SOG:     {(report.total_distance_nm / max(report.total_hours, 0.001)):.2f} kn")
        print(f"Fuel:        {report.total_fuel_tonnes:,.1f} t")
        print(f"Cost:        ${report.total_cost_usd:,.0f}")
        print(f"Storm hours: {report.storm_hours:.1f} h")
        if report.storm_warnings:
            print("\nWarnings:")
            for w in report.storm_warnings:
                print(f"  ! {w}")

    # Exports
    if args.export_gpx or args.export_nmea or args.export_json:
        from export_import import export_to_gpx, export_to_nmea, export_to_json
        route_out = report.route if report is not None else waypoints
        eta_list = None
        if report is not None:
            cumulative = 0.0
            eta_list = [start_dt]
            for seg in report.segments:
                cumulative += seg.hours
                eta_list.append(start_dt + timedelta(hours=cumulative))
        if args.export_gpx:
            export_to_gpx(args.export_gpx, waypoints, route_order=route_out, eta_list=eta_list)
            print(f"\nWrote GPX:  {args.export_gpx}")
        if args.export_nmea:
            export_to_nmea(args.export_nmea, route_out)
            print(f"Wrote NMEA: {args.export_nmea}")
        if args.export_json:
            export_to_json(args.export_json, route_out, eta_per_waypoint=eta_list)
            print(f"Wrote JSON: {args.export_json}")

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
        print("  marine-route-optimizer --cli [...]  # one-shot CLI; pass --cli --help for options")
        return 0
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
