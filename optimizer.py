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


# ---------------------------------------------------------------------------
# Weather-aware optimization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VesselParams:
    """Subset of vessel description used by the weather-aware optimizer."""

    type_key: str               # e.g. "container", "tanker" — see seakeeping.ALIASES
    v_max_kn: float             # service / calm-water speed
    length_m: float = 100.0     # waterline length
    draft_m: float = 6.0        # draft for bathymetry checks


@dataclass
class SegmentReport:
    """What the optimizer says about a single A→B leg."""

    a_index: int
    b_index: int
    distance_nm: float
    sog_kn: float
    hours: float
    wind_kn: float
    wave_m: float
    storm: bool
    navigable: bool
    min_depth_m: float
    fuel_tonnes: float
    cost_usd: float


@dataclass
class OptimizationReport:
    route: list[Waypoint]
    segments: list[SegmentReport]
    total_distance_nm: float
    total_hours: float
    total_fuel_tonnes: float
    total_cost_usd: float
    storm_hours: float
    storm_warnings: list[str]
    mode: str


def _segment_weather(weather_a, weather_b):
    """Average the wind/wave fields of both endpoints for a more honest leg."""
    import math as _m

    def avg_dir(d1: float, d2: float) -> float:
        # vector-mean of two directions
        r1, r2 = _m.radians(d1), _m.radians(d2)
        x = _m.cos(r1) + _m.cos(r2)
        y = _m.sin(r1) + _m.sin(r2)
        return (_m.degrees(_m.atan2(y, x)) + 360.0) % 360.0

    class _SegW:
        wind_speed_kn = (weather_a.wind_speed_kn + weather_b.wind_speed_kn) / 2.0
        wave_height_m = (weather_a.wave_height_m + weather_b.wave_height_m) / 2.0
        wind_direction_deg = avg_dir(weather_a.wind_direction_deg, weather_b.wind_direction_deg)
        wave_direction_deg = avg_dir(weather_a.wave_direction_deg, weather_b.wave_direction_deg)

    return _SegW


def _evaluate_segment(
    a: Waypoint,
    b: Waypoint,
    a_index: int,
    b_index: int,
    weather_at_a,                  # weather at departure point
    weather_at_b,                  # weather at arrival point
    vessel: VesselParams,
    econ,                          # economics.VesselEconomics
    min_clearance_m: float | None, # None = skip bathymetry
) -> SegmentReport:
    """Run the per-segment physics + economics."""
    from seakeeping import calculate_sog, heading_between
    from economics import consumption_at_speed
    from bathymetry import check_segment

    seg_w = _segment_weather(weather_at_a, weather_at_b)
    distance = haversine_nm(a, b)
    heading = heading_between(a.lat, a.lon, b.lat, b.lon)
    sk = calculate_sog(
        v_max=vessel.v_max_kn,
        vessel_type=vessel.type_key,
        length=vessel.length_m,
        draft=vessel.draft_m,
        wave_height=seg_w.wave_height_m,
        wind_speed=seg_w.wind_speed_kn,
        wave_direction=seg_w.wave_direction_deg,
        wind_direction=seg_w.wind_direction_deg,
        heading=heading,
    )
    hours = distance / max(sk.sog_kn, 0.1)
    days = hours / 24.0
    fuel = consumption_at_speed(econ, sk.sog_kn) * days

    if min_clearance_m is not None:
        bath = check_segment(a.lat, a.lon, b.lat, b.lon,
                             vessel_draft_m=vessel.draft_m,
                             min_clearance_m=min_clearance_m,
                             samples=8)
        navigable = bath.safe
        min_depth = bath.min_depth_m
    else:
        navigable = True
        min_depth = 0.0

    # Per-segment cost (economy framing). Storm tripling applied below.
    fuel_cost = fuel * econ.fuel_price_usd_per_tonne
    time_cost = hours * econ.hire_usd_per_hour
    storm_cost = (hours * econ.storm_penalty_usd_per_hour) if sk.storm_warning else 0.0
    seg_cost = fuel_cost + time_cost + storm_cost

    return SegmentReport(
        a_index=a_index,
        b_index=b_index,
        distance_nm=distance,
        sog_kn=sk.sog_kn,
        hours=hours,
        wind_kn=seg_w.wind_speed_kn,
        wave_m=seg_w.wave_height_m,
        storm=sk.storm_warning,
        navigable=navigable,
        min_depth_m=min_depth,
        fuel_tonnes=fuel,
        cost_usd=seg_cost,
    )


def _segment_weight(seg: SegmentReport, mode: str) -> float:
    """Weight used by 2-opt — returns infinity for non-navigable segments."""
    from economics import STORM_MULTIPLIER

    if not seg.navigable:
        return float("inf")
    multiplier = STORM_MULTIPLIER if seg.storm else 1.0
    if mode == "time":
        return seg.hours * multiplier
    if mode == "fuel":
        return seg.fuel_tonnes * multiplier
    if mode == "safety":
        return seg.distance_nm * (10.0 if seg.storm else 1.0)
    # default: economy
    return seg.cost_usd


def _total_weight(
    route: list[Waypoint], weather, vessel: VesselParams, econ, mode: str,
    min_clearance_m: float | None,
) -> float:
    if len(route) < 2:
        return 0.0
    total = 0.0
    for i in range(len(route) - 1):
        seg = _evaluate_segment(
            route[i], route[i + 1], i, i + 1,
            weather[i], weather[i + 1], vessel, econ, min_clearance_m,
        )
        w = _segment_weight(seg, mode)
        if w == float("inf"):
            return float("inf")
        total += w
    return total


def _two_opt_weighted(
    route: list[Waypoint], weather_by_index, vessel: VesselParams, econ,
    mode: str, min_clearance_m: float | None, max_passes: int = 30,
) -> list[Waypoint]:
    """2-opt with a weather-aware cost function.

    ``weather_by_index`` maps *original waypoint identity* to a sample (so a
    swap doesn't shuffle weather). We use ``id(wp)`` since Waypoint is frozen.
    """
    def weather_for(seq: list[Waypoint]) -> list:
        return [weather_by_index[id(p)] for p in seq]

    best = route
    passes = 0
    while passes < max_passes:
        improved = False
        passes += 1
        base = _total_weight(best, weather_for(best), vessel, econ, mode, min_clearance_m)
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best)):
                if j - i == 1:
                    continue
                cand = best[:i] + best[i:j][::-1] + best[j:]
                cand_w = _total_weight(cand, weather_for(cand), vessel, econ, mode, min_clearance_m)
                if cand_w + 1e-9 < base:
                    best = cand
                    base = cand_w
                    improved = True
        if not improved:
            break
    return best


def optimize_route_with_weather(
    waypoints: list[Waypoint],
    vessel: VesselParams,
    weather: list,                              # list[WeatherSample] parallel to waypoints
    mode: str = "economy",                      # "time" | "fuel" | "safety" | "economy"
    econ=None,                                  # economics.VesselEconomics; default by vessel.type_key
    min_clearance_m: float | None = None,       # None = bathymetry disabled
    return_to_start: bool = False,
) -> OptimizationReport:
    """Re-order waypoints minimizing the selected cost function under weather.

    Weather is fixed per waypoint at call time; this function does NOT
    refetch forecasts after each swap (that would explode cost). Apply
    weather updates by re-running optimization with fresh samples.
    """
    from economics import ECONOMICS_PRESETS

    if len(waypoints) < 2:
        return OptimizationReport(
            route=list(waypoints), segments=[], total_distance_nm=0.0,
            total_hours=0.0, total_fuel_tonnes=0.0, total_cost_usd=0.0,
            storm_hours=0.0, storm_warnings=[], mode=mode,
        )

    if econ is None:
        from seakeeping import normalize_vessel_type
        econ = ECONOMICS_PRESETS.get(normalize_vessel_type(vessel.type_key), ECONOMICS_PRESETS["generic"])

    if len(weather) != len(waypoints):
        raise ValueError(f"weather length {len(weather)} != waypoints length {len(waypoints)}")

    weather_map = {id(wp): w for wp, w in zip(waypoints, weather)}

    # Multi-start nearest-neighbor seeding, then weighted 2-opt.
    best_route: list[Waypoint] | None = None
    best_weight = math.inf
    for start in range(min(len(waypoints), 4)):
        seeded = nearest_neighbor(waypoints, start_index=start)
        refined = _two_opt_weighted(seeded, weather_map, vessel, econ, mode, min_clearance_m)
        w = _total_weight(refined, [weather_map[id(p)] for p in refined], vessel, econ, mode, min_clearance_m)
        if w < best_weight:
            best_weight = w
            best_route = refined
    assert best_route is not None

    if return_to_start:
        best_route = best_route + [best_route[0]]

    # Build segment report with the *original* per-waypoint weather samples.
    segments: list[SegmentReport] = []
    total_distance = 0.0
    total_hours = 0.0
    total_fuel = 0.0
    total_cost = 0.0
    storm_hours = 0.0
    storm_warnings: list[str] = []
    for i in range(len(best_route) - 1):
        a = best_route[i]
        b = best_route[i + 1]
        seg = _evaluate_segment(
            a, b, i, i + 1,
            weather_map[id(a)], weather_map[id(b)],
            vessel, econ, min_clearance_m,
        )
        segments.append(seg)
        total_distance += seg.distance_nm
        total_hours += seg.hours
        total_fuel += seg.fuel_tonnes
        total_cost += seg.cost_usd
        if seg.storm:
            storm_hours += seg.hours
            storm_warnings.append(
                f"Leg {i + 1} ({a.name} → {b.name}): Hs {seg.wave_m:.1f} m, wind {seg.wind_kn:.0f} kn"
            )
        if not seg.navigable:
            storm_warnings.append(
                f"Leg {i + 1} ({a.name} → {b.name}): SHALLOW — min depth {seg.min_depth_m:.0f} m"
            )

    return OptimizationReport(
        route=best_route,
        segments=segments,
        total_distance_nm=total_distance,
        total_hours=total_hours,
        total_fuel_tonnes=total_fuel,
        total_cost_usd=total_cost,
        storm_hours=storm_hours,
        storm_warnings=storm_warnings,
        mode=mode,
    )
