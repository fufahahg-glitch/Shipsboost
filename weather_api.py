"""Weather data provider for the Marine Route Optimizer.

Two layered sources of wind/wave data:

1. **Open-Meteo Marine API** (free, no API key, JSON over HTTPS).
2. **Climatology fallback** — a deterministic latitudinal model used when
   the network is unavailable or the API errors out. Good enough for a
   smoke run; clearly labelled as ``source="climate"``.

Results are cached on disk via ``pickle`` keyed by (lat-rounded, lon-rounded,
forecast-hour). This keeps repeated route optimizations from hammering the
API and makes the GUI behave offline after a single successful fetch.

No third-party dependencies — uses ``urllib`` from the stdlib.
"""

from __future__ import annotations

import json
import math
import pickle
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional


CACHE_PATH = Path.home() / ".marine_route_optimizer_weather_cache.pkl"
CACHE_TTL_SECONDS = 6 * 3600  # 6 hours — marine forecasts update every cycle
OPEN_METEO_MARINE = "https://marine-api.open-meteo.com/v1/marine"
OPEN_METEO_WEATHER = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_S = 8.0


@dataclass
class WeatherSample:
    """Conditions at a single waypoint and forecast hour."""

    lat: float
    lon: float
    valid_at: str               # ISO-8601 UTC
    wind_speed_kn: float        # knots
    wind_direction_deg: float   # degrees true, direction the wind is coming FROM
    wave_height_m: float        # significant wave height [m]
    wave_direction_deg: float   # direction waves are coming FROM
    wave_period_s: float        # mean wave period [s]
    source: str                 # "open-meteo" | "climate" | "grib" | "manual"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("rb") as fh:
            return pickle.load(fh)
    except (pickle.UnpicklingError, EOFError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        with CACHE_PATH.open("wb") as fh:
            pickle.dump(cache, fh)
    except OSError:
        pass


def _cache_key(lat: float, lon: float, forecast_hour: int) -> tuple[float, float, int]:
    return (round(lat, 2), round(lon, 2), forecast_hour)


def clear_cache() -> None:
    with _cache_lock:
        if CACHE_PATH.exists():
            try:
                CACHE_PATH.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Climatology fallback
# ---------------------------------------------------------------------------

def _climate_sample(lat: float, lon: float, valid_at: datetime) -> WeatherSample:
    """Latitude-based climatology with seasonal modulation.

    Models the dominant zonal wind belts (trades, westerlies, polar easterlies)
    and seasonal storminess. Returns plausible — not accurate — values so the
    rest of the pipeline always has something to chew on.
    """
    abs_lat = abs(lat)
    season = math.sin((valid_at.timetuple().tm_yday / 365.0 - 0.25) * 2.0 * math.pi)
    hemisphere = 1.0 if lat >= 0 else -1.0

    # Zonal wind speed (kn): trade/westerly maxima around 15° and 45° latitude.
    base_wind = (
        18.0 * math.exp(-((abs_lat - 45.0) / 12.0) ** 2)   # westerlies
        + 12.0 * math.exp(-((abs_lat - 15.0) / 8.0) ** 2)  # trades
        + 6.0
    )
    seasonal_boost = 6.0 * max(0.0, -hemisphere * season)  # winter hemisphere
    wind_speed = base_wind + seasonal_boost

    # Wind direction: westerlies blow from W (270°) in NH, trades from NE (~60°).
    if abs_lat >= 30:
        wind_dir = 270.0 if lat >= 0 else 270.0 + 30.0
    elif abs_lat >= 5:
        wind_dir = 60.0 if lat >= 0 else 130.0
    else:
        wind_dir = 90.0  # equatorial easterlies

    # Wave height scales with wind, gentler near coasts (proxied by lon noise).
    fetch_factor = 0.7 + 0.3 * abs(math.sin(math.radians(lon)))
    hs = max(0.4, 0.04 * wind_speed * wind_speed * fetch_factor / 10.0)
    hs = min(hs, 9.0)
    period = 4.0 + math.sqrt(hs) * 2.0
    wave_dir = (wind_dir + 10.0) % 360.0

    return WeatherSample(
        lat=lat,
        lon=lon,
        valid_at=valid_at.replace(tzinfo=timezone.utc).isoformat(),
        wind_speed_kn=round(wind_speed, 1),
        wind_direction_deg=round(wind_dir, 0),
        wave_height_m=round(hs, 2),
        wave_direction_deg=round(wave_dir, 0),
        wave_period_s=round(period, 1),
        source="climate",
    )


# ---------------------------------------------------------------------------
# Open-Meteo HTTP fetch
# ---------------------------------------------------------------------------

def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "MarineRouteOptimizer/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _open_meteo_sample(lat: float, lon: float, valid_at: datetime) -> WeatherSample | None:
    """Single-point fetch. Returns None on any failure (caller falls back)."""
    iso_hour = valid_at.strftime("%Y-%m-%dT%H:00")
    start = valid_at.strftime("%Y-%m-%d")
    end = (valid_at + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        marine_q = urllib.parse.urlencode({
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "hourly": "wave_height,wave_direction,wave_period",
            "start_date": start,
            "end_date": end,
            "timezone": "UTC",
        })
        weather_q = urllib.parse.urlencode({
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kn",
            "start_date": start,
            "end_date": end,
            "timezone": "UTC",
        })
        marine = _http_get_json(f"{OPEN_METEO_MARINE}?{marine_q}")
        wind = _http_get_json(f"{OPEN_METEO_WEATHER}?{weather_q}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    try:
        marine_times: list[str] = marine["hourly"]["time"]
        wind_times: list[str] = wind["hourly"]["time"]
        m_idx = marine_times.index(iso_hour) if iso_hour in marine_times else 0
        w_idx = wind_times.index(iso_hour) if iso_hour in wind_times else 0
        hs = float(marine["hourly"]["wave_height"][m_idx] or 0.0)
        wave_dir = float(marine["hourly"]["wave_direction"][m_idx] or 0.0)
        wave_per = float(marine["hourly"]["wave_period"][m_idx] or 0.0)
        ws = float(wind["hourly"]["wind_speed_10m"][w_idx] or 0.0)
        wd = float(wind["hourly"]["wind_direction_10m"][w_idx] or 0.0)
    except (KeyError, IndexError, TypeError, ValueError):
        return None

    return WeatherSample(
        lat=lat,
        lon=lon,
        valid_at=valid_at.replace(tzinfo=timezone.utc).isoformat(),
        wind_speed_kn=round(ws, 1),
        wind_direction_deg=round(wd, 0),
        wave_height_m=round(hs, 2),
        wave_direction_deg=round(wave_dir, 0),
        wave_period_s=round(wave_per, 1),
        source="open-meteo",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_weather_at(
    lat: float,
    lon: float,
    valid_at: datetime,
    use_api: bool = True,
) -> WeatherSample:
    """Return a single WeatherSample, preferring live API, falling back to climate."""
    forecast_hour = int((valid_at - datetime.now(tz=timezone.utc)).total_seconds() // 3600)
    key = _cache_key(lat, lon, forecast_hour)
    now = time.time()

    with _cache_lock:
        cache = _load_cache()
        entry = cache.get(key)
        if entry and now - entry[0] < CACHE_TTL_SECONDS:
            return entry[1]

    sample: WeatherSample | None = None
    if use_api:
        sample = _open_meteo_sample(lat, lon, valid_at)
    if sample is None:
        sample = _climate_sample(lat, lon, valid_at)

    with _cache_lock:
        cache = _load_cache()
        cache[key] = (now, sample)
        _save_cache(cache)

    return sample


def get_weather_for_route(
    waypoints: Iterable,                          # list of objects with .lat/.lon (Waypoint)
    start_datetime: datetime,
    vessel_speed_kn: float = 15.0,
    use_api: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[WeatherSample]:
    """Fetch weather for each waypoint, offsetting valid time by expected ETA.

    The ETA at waypoint i is estimated from the cumulative great-circle
    distance to that point divided by ``vessel_speed_kn``.
    """
    from optimizer import haversine_nm  # local import to avoid cycles at module load

    pts = list(waypoints)
    out: list[WeatherSample] = []
    cumulative_nm = 0.0
    prev = None
    for i, wp in enumerate(pts):
        if prev is not None:
            cumulative_nm += haversine_nm(prev, wp)
        eta_hours = cumulative_nm / max(vessel_speed_kn, 1.0)
        valid_at = start_datetime + timedelta(hours=eta_hours)
        sample = get_weather_at(wp.lat, wp.lon, valid_at, use_api=use_api)
        out.append(sample)
        if progress_cb is not None:
            try:
                progress_cb(i + 1, len(pts))
            except Exception:
                pass
        prev = wp
    return out


def fetch_async(
    waypoints,
    start_datetime: datetime,
    vessel_speed_kn: float,
    use_api: bool,
    on_done: Callable[[list[WeatherSample]], None],
    on_error: Callable[[Exception], None] | None = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> threading.Thread:
    """Fire-and-forget thread wrapper for GUI integration."""

    def runner() -> None:
        try:
            result = get_weather_for_route(
                waypoints, start_datetime, vessel_speed_kn, use_api, progress_cb
            )
            on_done(result)
        except Exception as exc:  # pragma: no cover — defensive
            if on_error is not None:
                on_error(exc)

    t = threading.Thread(target=runner, daemon=True, name="weather-fetch")
    t.start()
    return t
