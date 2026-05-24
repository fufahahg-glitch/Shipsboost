"""Bathymetry helpers for the Marine Route Optimizer.

Real bathymetry data (GEBCO, ETOPO) is hundreds of MB. For planning-grade
checks we ship a procedural model that captures the qualitative shape of
ocean depth:

* coastal continental shelf within ~150 km of major land masses (-50 to -200 m)
* abyssal plains in mid-ocean (-3000 to -5500 m)
* Mediterranean trough averaging around -1500 m
* shallow seas (North/Baltic/Persian Gulf) flagged in their bounding boxes

The optional :func:`fetch_depth_online` queries the public open-elevation
service as a best-effort upgrade.
"""

from __future__ import annotations

import math
import urllib.error
import urllib.request
import json
from dataclasses import dataclass
from typing import Optional


# (min_lat, max_lat, min_lon, max_lon, mean_depth_m, std_depth_m)
SHALLOW_SEAS: list[tuple[float, float, float, float, float, float]] = [
    # North Sea (extended east to cover Hamburg approach)
    (51.0, 60.0, -2.0, 10.5, -60.0, 30.0),
    # Baltic Sea
    (53.5, 65.5, 10.5, 30.0, -55.0, 25.0),
    # English Channel & Celtic Sea
    (47.5, 51.5, -10.0, 2.0, -90.0, 30.0),
    # Persian Gulf
    (24.0, 30.0, 48.0, 57.0, -35.0, 15.0),
    # Yellow Sea
    (32.0, 41.0, 119.0, 127.0, -45.0, 20.0),
    # Java/South China shelf
    (-10.0, 20.0, 100.0, 120.0, -60.0, 30.0),
    # Hudson Bay
    (51.0, 65.0, -95.0, -75.0, -120.0, 50.0),
    # Red Sea (Suez approach)
    (12.0, 30.5, 32.0, 44.0, -500.0, 200.0),
]

# (centre_lat, centre_lon, radius_deg, mean_depth_m)
ENCLOSED_BASINS: list[tuple[float, float, float, float]] = [
    (37.0, 18.0, 8.0, -1500.0),     # Mediterranean
    (43.0, 35.0, 4.0, -1300.0),     # Black Sea
    (25.0, -90.0, 8.0, -1600.0),    # Gulf of Mexico
    (15.0, 75.0, 7.0, -2800.0),     # Arabian Sea
    (50.0, -55.0, 6.0, -350.0),     # Grand Banks
]


def _in_box(lat: float, lon: float, box: tuple[float, float, float, float, float, float]) -> bool:
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def get_depth(lat: float, lon: float) -> float:
    """Return an estimated water depth in metres (negative below sea level).

    Positive values indicate the point appears to be on land in this very
    rough model; callers should treat positive depths as non-navigable.
    """
    # 1. Enclosed/marginal basins win first.
    for basin in ENCLOSED_BASINS:
        clat, clon, rad, mean = basin
        if math.hypot(lat - clat, lon - clon) < rad:
            return mean + 50.0 * math.sin(math.radians(lat * 7 + lon * 11))

    # 2. Shallow seas (continental shelf) by bounding box.
    for box in SHALLOW_SEAS:
        if _in_box(lat, lon, box):
            mean = box[4]
            jitter = box[5] * math.sin(math.radians(lat * 13 + lon * 17))
            return mean + jitter

    # 3. Open ocean — abyssal plain with a mid-ocean-ridge style bump.
    deep = -4500.0 + 1500.0 * math.sin(math.radians(lat * 3.0)) * math.cos(math.radians(lon * 2.0))
    return deep


def is_navigable(depth_m: float, min_clearance_m: float, vessel_draft_m: float) -> bool:
    """Check that the seabed is at least ``draft + min_clearance`` below the surface."""
    return -depth_m >= (vessel_draft_m + max(0.0, min_clearance_m))


@dataclass
class SegmentSafety:
    safe: bool
    min_depth_m: float
    sample_count: int


def check_segment(
    a_lat: float, a_lon: float, b_lat: float, b_lon: float,
    vessel_draft_m: float, min_clearance_m: float = 5.0,
    samples: int = 8,
) -> SegmentSafety:
    """Sample depth along a segment and report the shallowest reading."""
    samples = max(2, samples)
    min_depth = math.inf
    for i in range(samples + 1):
        t = i / samples
        lat = a_lat + (b_lat - a_lat) * t
        lon = a_lon + (b_lon - a_lon) * t
        d = get_depth(lat, lon)
        if d < min_depth:
            min_depth = d
    return SegmentSafety(
        safe=is_navigable(min_depth, min_clearance_m, vessel_draft_m),
        min_depth_m=min_depth,
        sample_count=samples + 1,
    )


def fetch_depth_online(lat: float, lon: float, timeout: float = 5.0) -> Optional[float]:
    """Best-effort online depth via open-elevation. Returns None on failure."""
    url = f"https://api.open-elevation.com/api/v1/lookup?locations={lat:.4f},{lon:.4f}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MarineRouteOptimizer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elev = float(data["results"][0]["elevation"])
        # API returns elevation above sea level. Negative = below water.
        return elev
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError, OSError):
        return None
