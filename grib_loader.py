"""GRIB1/GRIB2 loader for the Marine Route Optimizer.

GRIB files are the standard format for numerical weather prediction output.
A *full* decoder requires either ``pygrib`` (linked against ecCodes) or
``cfgrib`` + ``xarray`` — both bring native dependencies that would make the
PyInstaller bundle huge. We therefore implement the loader in two layers:

1. **Header probe (always available)** — parses the GRIB Section 0 indicator
   to confirm the file is real GRIB and reports edition / total length /
   discipline. This is enough for the GUI to display a "file recognized"
   confirmation and offer a list of contained messages.

2. **Optional full decode** — if ``pygrib`` is importable on the target
   system, ``load_grib_file()`` returns a list of GRIBField with sampled
   values. Otherwise it returns the header-only stub.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from weather_api import WeatherSample


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GRIBMessageHeader:
    edition: int          # 1 or 2
    length_bytes: int
    discipline: int       # GRIB2 only (0=meteorological, 10=oceanographic)
    offset: int


@dataclass
class GRIBField:
    """Decoded field (only populated when pygrib is available)."""

    short_name: str
    long_name: str
    valid_at: datetime
    grid_shape: tuple[int, int]
    lats: list[float] = field(default_factory=list)
    lons: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


@dataclass
class GRIBFile:
    path: Path
    messages: list[GRIBMessageHeader]
    fields: list[GRIBField]
    decoded: bool
    notice: str = ""


# ---------------------------------------------------------------------------
# Stdlib header probe — no third-party deps
# ---------------------------------------------------------------------------

def _scan_grib_headers(path: Path, max_messages: int = 64) -> list[GRIBMessageHeader]:
    """Walk the file looking for ``GRIB`` markers. Returns up to ``max_messages``."""
    out: list[GRIBMessageHeader] = []
    with path.open("rb") as fh:
        data = fh.read()

    i = 0
    n = len(data)
    while i < n - 16 and len(out) < max_messages:
        if data[i:i + 4] != b"GRIB":
            i += 1
            continue
        # GRIB2 Section 0: 'GRIB' (4) | reserved (2) | discipline (1) | edition (1) | length (8)
        # GRIB1 Section 0: 'GRIB' (4) | length (3)                    | edition (1)
        edition = data[i + 7]
        if edition == 2:
            discipline = data[i + 6]
            length = struct.unpack(">Q", data[i + 8:i + 16])[0]
            out.append(GRIBMessageHeader(edition=2, length_bytes=length, discipline=discipline, offset=i))
            i += max(length, 16)
        elif edition == 1:
            length = int.from_bytes(data[i + 4:i + 7], "big")
            out.append(GRIBMessageHeader(edition=1, length_bytes=length, discipline=0, offset=i))
            i += max(length, 8)
        else:
            i += 4
    return out


# ---------------------------------------------------------------------------
# Optional full decode via pygrib
# ---------------------------------------------------------------------------

def _try_pygrib_decode(path: Path) -> tuple[list[GRIBField], str]:
    try:
        import pygrib  # type: ignore[import-not-found]
    except ImportError:
        return [], "pygrib not installed — header-only mode"

    fields: list[GRIBField] = []
    try:
        grbs = pygrib.open(str(path))
        try:
            for msg in grbs:
                try:
                    lats, lons = msg.latlons()
                    vals = msg.values
                    shape = (int(getattr(vals, "shape", (0,))[0] or 0), int(getattr(vals, "shape", (0, 0))[-1] or 0))
                    field = GRIBField(
                        short_name=str(msg.shortName),
                        long_name=str(msg.name),
                        valid_at=datetime(
                            msg.year, msg.month, msg.day, msg.hour, getattr(msg, "minute", 0) or 0,
                            tzinfo=timezone.utc,
                        ),
                        grid_shape=shape,
                        lats=[float(v) for v in (lats.ravel().tolist() if hasattr(lats, "ravel") else lats)],
                        lons=[float(v) for v in (lons.ravel().tolist() if hasattr(lons, "ravel") else lons)],
                        values=[float(v) for v in (vals.ravel().tolist() if hasattr(vals, "ravel") else vals)],
                    )
                    fields.append(field)
                except Exception:  # skip individual broken messages
                    continue
        finally:
            grbs.close()
    except Exception as exc:
        return [], f"pygrib decode failed: {exc}"
    return fields, "decoded with pygrib"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_grib_file(path: str | Path) -> GRIBFile:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    headers = _scan_grib_headers(p)
    if not headers:
        return GRIBFile(path=p, messages=[], fields=[], decoded=False,
                        notice="No 'GRIB' indicator found — file is not GRIB1/GRIB2.")
    fields, notice = _try_pygrib_decode(p)
    return GRIBFile(path=p, messages=headers, fields=fields, decoded=bool(fields), notice=notice)


_WAVE_NAMES = {"swh", "shww", "hs", "wave_height", "significant height of combined wind waves and swell"}
_WIND_U_NAMES = {"10u", "u10", "ugrd"}
_WIND_V_NAMES = {"10v", "v10", "vgrd"}


def _nearest_value(field: GRIBField, lat: float, lon: float) -> float | None:
    if not field.values:
        return None
    best = None
    best_d = math.inf
    for la, lo, val in zip(field.lats, field.lons, field.values):
        d = (la - lat) ** 2 + ((lo - lon + 180) % 360 - 180) ** 2
        if d < best_d:
            best_d = d
            best = val
    return best


def samples_from_grib(grib: GRIBFile, waypoints: Iterable, valid_at: datetime) -> list[WeatherSample]:
    """Extract WeatherSamples from a decoded GRIB. Falls back to zeros on header-only."""
    out: list[WeatherSample] = []
    waves = next((f for f in grib.fields if f.short_name.lower() in _WAVE_NAMES), None)
    u_wind = next((f for f in grib.fields if f.short_name.lower() in _WIND_U_NAMES), None)
    v_wind = next((f for f in grib.fields if f.short_name.lower() in _WIND_V_NAMES), None)
    iso = valid_at.replace(tzinfo=timezone.utc).isoformat()
    for wp in waypoints:
        hs = _nearest_value(waves, wp.lat, wp.lon) if waves else 0.0
        u = _nearest_value(u_wind, wp.lat, wp.lon) if u_wind else 0.0
        v = _nearest_value(v_wind, wp.lat, wp.lon) if v_wind else 0.0
        ws_ms = math.hypot(u or 0.0, v or 0.0)
        ws_kn = ws_ms * 1.9438
        wd = (math.degrees(math.atan2(-(u or 0.0), -(v or 0.0))) + 360.0) % 360.0
        out.append(WeatherSample(
            lat=wp.lat,
            lon=wp.lon,
            valid_at=iso,
            wind_speed_kn=round(ws_kn, 1),
            wind_direction_deg=round(wd, 0),
            wave_height_m=round(float(hs or 0.0), 2),
            wave_direction_deg=round(wd, 0),  # GRIB rarely has explicit wave-dir
            wave_period_s=0.0,
            source="grib",
        ))
    return out
