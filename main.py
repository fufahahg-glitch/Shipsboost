"""Marine Route Optimizer AI.

Computes a near-optimal route through a set of waypoints (latitude, longitude)
using nearest-neighbor construction followed by 2-opt refinement. Distance is
the great-circle (haversine) distance, which is appropriate for ocean travel.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EARTH_RADIUS_NM = 3440.065  # nautical miles


@dataclass(frozen=True)
class Waypoint:
    name: str
    lat: float
    lon: float


def haversine_nm(a: Waypoint, b: Waypoint) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(h))


def route_length(route: list[Waypoint]) -> float:
    return sum(haversine_nm(route[i], route[i + 1]) for i in range(len(route) - 1))


def nearest_neighbor(points: list[Waypoint], start_index: int = 0) -> list[Waypoint]:
    remaining = points.copy()
    current = remaining.pop(start_index)
    ordered = [current]
    while remaining:
        next_idx = min(range(len(remaining)), key=lambda i: haversine_nm(current, remaining[i]))
        current = remaining.pop(next_idx)
        ordered.append(current)
    return ordered


def two_opt(route: list[Waypoint], max_passes: int = 50) -> list[Waypoint]:
    best = route
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best)):
                if j - i == 1:
                    continue
                candidate = best[:i] + best[i:j][::-1] + best[j:]
                if route_length(candidate) + 1e-9 < route_length(best):
                    best = candidate
                    improved = True
        if not improved:
            break
    return best


def optimize(points: list[Waypoint], return_to_start: bool = False) -> tuple[list[Waypoint], float]:
    if len(points) < 2:
        return points, 0.0
    seeded = nearest_neighbor(points)
    refined = two_opt(seeded)
    if return_to_start:
        refined = refined + [refined[0]]
    return refined, route_length(refined)


def load_waypoints(path: Path) -> list[Waypoint]:
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return [Waypoint(d["name"], float(d["lat"]), float(d["lon"])) for d in data]
    # CSV: name,lat,lon
    rows: Iterable[list[str]] = csv.reader(text.splitlines())
    waypoints: list[Waypoint] = []
    for row in rows:
        if not row or row[0].lstrip().startswith("#"):
            continue
        if row[0].strip().lower() == "name":
            continue
        name, lat, lon = row[0].strip(), float(row[1]), float(row[2])
        waypoints.append(Waypoint(name, lat, lon))
    return waypoints


def format_route(route: list[Waypoint], total_nm: float) -> str:
    lines = ["Optimized route:"]
    for i, wp in enumerate(route, start=1):
        lines.append(f"  {i:>3}. {wp.name:<24} ({wp.lat:+.4f}, {wp.lon:+.4f})")
    lines.append("")
    lines.append(f"Total distance: {total_nm:,.1f} nautical miles ({total_nm * 1.852:,.1f} km)")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Marine Route Optimizer AI")
    parser.add_argument("input", nargs="?", help="CSV or JSON file with waypoints (name,lat,lon)")
    parser.add_argument("--loop", action="store_true", help="Return to the starting waypoint")
    parser.add_argument("--demo", action="store_true", help="Run with a built-in demo dataset")
    return parser.parse_args(argv)


DEMO_WAYPOINTS = [
    Waypoint("Rotterdam", 51.9225, 4.4792),
    Waypoint("Lisbon", 38.7223, -9.1393),
    Waypoint("Gibraltar", 36.1408, -5.3536),
    Waypoint("Algiers", 36.7538, 3.0588),
    Waypoint("Piraeus", 37.9420, 23.6469),
    Waypoint("Alexandria", 31.2001, 29.9187),
    Waypoint("Suez", 29.9668, 32.5498),
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
    print(f"Input: {source} ({len(waypoints)} waypoints)\n")
    print(format_route(route, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
