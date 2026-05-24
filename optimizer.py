"""Pure-stdlib core for the Marine Route Optimizer.

Kept free of any GUI imports so it can be unit-tested and reused from the CLI
or the Tk frontend.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EARTH_RADIUS_NM = 3440.065  # nautical miles
NM_PER_KM = 1 / 1.852


@dataclass(frozen=True)
class Waypoint:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class VesselType:
    name: str
    cruise_knots: float


VESSEL_TYPES: tuple[VesselType, ...] = (
    VesselType("Container ship", 22.0),
    VesselType("Bulk carrier", 14.0),
    VesselType("Oil tanker", 15.0),
    VesselType("Cruise ship", 22.0),
    VesselType("Motor yacht", 18.0),
    VesselType("Sailing yacht", 8.0),
    VesselType("Tug boat", 12.0),
    VesselType("Fishing vessel", 10.0),
)


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
    passes = 0
    while passes < max_passes:
        improved = False
        passes += 1
        best_len = route_length(best)
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best)):
                if j - i == 1:
                    continue
                candidate = best[:i] + best[i:j][::-1] + best[j:]
                cand_len = route_length(candidate)
                if cand_len + 1e-9 < best_len:
                    best = candidate
                    best_len = cand_len
                    improved = True
        if not improved:
            break
    return best


def optimize(points: list[Waypoint], return_to_start: bool = False) -> tuple[list[Waypoint], float]:
    if len(points) < 2:
        return list(points), 0.0
    # Try each starting point and keep the best — cheap on small inputs and
    # noticeably improves quality vs. fixing index 0.
    best_route: list[Waypoint] | None = None
    best_len = math.inf
    candidates = range(min(len(points), 8))
    for start in candidates:
        seeded = nearest_neighbor(points, start_index=start)
        refined = two_opt(seeded)
        length = route_length(refined)
        if length < best_len:
            best_len = length
            best_route = refined
    assert best_route is not None
    if return_to_start:
        best_route = best_route + [best_route[0]]
        best_len = route_length(best_route)
    return best_route, best_len


def load_waypoints(path: Path) -> list[Waypoint]:
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        return [Waypoint(d["name"], float(d["lat"]), float(d["lon"])) for d in data]
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


def save_waypoints_csv(path: Path, waypoints: list[Waypoint]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", "lat", "lon"])
        for wp in waypoints:
            writer.writerow([wp.name, f"{wp.lat:.6f}", f"{wp.lon:.6f}"])


def format_duration(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} min"
    days, rem_h = divmod(hours, 24)
    if days >= 1:
        return f"{int(days)}d {rem_h:.1f}h"
    return f"{hours:.1f} h"


def estimate_duration_hours(distance_nm: float, speed_knots: float) -> float:
    if speed_knots <= 0:
        return float("inf")
    return distance_nm / speed_knots


DEMO_WAYPOINTS: list[Waypoint] = [
    Waypoint("Rotterdam", 51.9225, 4.4792),
    Waypoint("Lisbon", 38.7223, -9.1393),
    Waypoint("Gibraltar", 36.1408, -5.3536),
    Waypoint("Algiers", 36.7538, 3.0588),
    Waypoint("Piraeus", 37.9420, 23.6469),
    Waypoint("Alexandria", 31.2001, 29.9187),
    Waypoint("Suez", 29.9668, 32.5498),
]
