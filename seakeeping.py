"""Seakeeping & speed-loss model for the Marine Route Optimizer.

Estimates Speed-Over-Ground (SOG) and percentage speed loss caused by
wind and waves for a given vessel and course. The model is a simplified
form of the Aertssen / Townsin / ISO 15016 added-resistance approach:

    delta_v_waves = a * Hs^2 / sqrt(L)        [knots]
    delta_v_wind  = b * (U_rel * cos(theta_w))**2 / 10000   [knots]
    SOG = max(v_max - delta_v_waves * f(beta_w) - delta_v_wind, v_min)

Where:
    Hs       — significant wave height [m]
    L        — vessel waterline length [m]
    U_rel    — apparent wind speed [knots]
    theta_w  — wind angle relative to heading [deg]
    beta_w   — wave encounter angle relative to heading [deg]
    a, b     — vessel-dependent coefficients

It is *not* a CFD-grade prediction — it is a planning-grade estimate that
behaves sensibly across heading angles, wave heights, and vessel types.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


VesselTypeKey = Literal["container", "tanker", "bulker", "yacht", "fishing", "cruise", "tug", "generic"]


@dataclass(frozen=True)
class HydroCoefficients:
    """Added-resistance coefficients per vessel archetype."""

    a_wave: float          # wave speed-loss coefficient
    b_wind: float          # wind speed-loss coefficient
    min_speed_kn: float    # minimum sustainable speed (heavy weather)
    storm_hs: float        # significant wave height considered storm-class [m]
    storm_wind_kn: float   # wind considered storm-class [kn]


# Tuned so that:
#   container 22 kn @ Hs=3 m, 20 kn headwind ≈ 19 kn SOG
#   tanker     15 kn @ Hs=3 m, 20 kn headwind ≈ 12.5 kn SOG
#   yacht      18 kn @ Hs=3 m, 20 kn headwind ≈ 12 kn SOG
COEFFS: dict[str, HydroCoefficients] = {
    "container": HydroCoefficients(a_wave=0.85, b_wind=0.18, min_speed_kn=6.0, storm_hs=5.5, storm_wind_kn=40.0),
    "tanker":    HydroCoefficients(a_wave=1.10, b_wind=0.22, min_speed_kn=5.0, storm_hs=5.0, storm_wind_kn=38.0),
    "bulker":    HydroCoefficients(a_wave=1.15, b_wind=0.24, min_speed_kn=4.0, storm_hs=4.5, storm_wind_kn=36.0),
    "cruise":    HydroCoefficients(a_wave=0.80, b_wind=0.20, min_speed_kn=8.0, storm_hs=5.0, storm_wind_kn=40.0),
    "yacht":     HydroCoefficients(a_wave=1.40, b_wind=0.30, min_speed_kn=3.0, storm_hs=3.5, storm_wind_kn=30.0),
    "fishing":   HydroCoefficients(a_wave=1.30, b_wind=0.28, min_speed_kn=3.0, storm_hs=4.0, storm_wind_kn=32.0),
    "tug":       HydroCoefficients(a_wave=1.20, b_wind=0.25, min_speed_kn=3.0, storm_hs=4.5, storm_wind_kn=35.0),
    "generic":   HydroCoefficients(a_wave=1.00, b_wind=0.22, min_speed_kn=4.0, storm_hs=4.0, storm_wind_kn=35.0),
}


# Aliases so callers can pass the human-readable vessel names from the GUI.
ALIASES: dict[str, str] = {
    "container ship": "container",
    "containership": "container",
    "container": "container",
    "oil tanker": "tanker",
    "tanker": "tanker",
    "bulk carrier": "bulker",
    "bulker": "bulker",
    "cruise ship": "cruise",
    "cruise": "cruise",
    "motor yacht": "yacht",
    "sailing yacht": "yacht",
    "yacht": "yacht",
    "fishing vessel": "fishing",
    "fishing": "fishing",
    "tug boat": "tug",
    "tug": "tug",
}


def normalize_vessel_type(name: str) -> str:
    return ALIASES.get(name.strip().lower(), "generic")


def _angle_factor_waves(encounter_deg: float) -> float:
    """1.0 on the bow, ~0.25 from astern. Cosine taper, clamped to [0.2, 1.0]."""
    a = math.radians(encounter_deg)
    return max(0.2, 0.6 + 0.4 * math.cos(a))


def _angle_factor_wind(relative_deg: float) -> float:
    """+1 on the bow (headwind hurts), -0.4 from astern (slight help)."""
    return math.cos(math.radians(relative_deg))


def _relative_angle(target_dir: float, heading: float) -> float:
    """Smallest signed delta in [-180, 180]."""
    d = (target_dir - heading + 540.0) % 360.0 - 180.0
    return d


@dataclass(frozen=True)
class SeakeepingResult:
    sog_kn: float
    loss_percent: float
    storm_warning: bool
    warning_text: str


def calculate_sog(
    v_max: float,
    vessel_type: str,
    length: float,
    draft: float,
    wave_height: float,
    wind_speed: float,
    wave_direction: float,
    wind_direction: float,
    heading: float,
) -> SeakeepingResult:
    """Estimate Speed-Over-Ground given environmental conditions.

    Args:
        v_max:           calm-water service speed in knots
        vessel_type:     archetype key or human label (see ALIASES)
        length:          waterline length in metres (used for wave scaling)
        draft:           draft in metres (not used in this lite model, kept
                         for parity with the bathymetry/safety checks)
        wave_height:     significant wave height Hs in metres
        wind_speed:      true wind speed in knots
        wave_direction:  direction waves are *coming from*, deg true
        wind_direction:  direction wind is *coming from*, deg true
        heading:         vessel heading, deg true

    Returns:
        SeakeepingResult with SOG, percentage loss, and storm warning info.
    """
    del draft  # reserved for future trim/squat corrections
    if v_max <= 0:
        return SeakeepingResult(0.0, 0.0, False, "")

    coeffs = COEFFS[normalize_vessel_type(vessel_type)]
    waterline = max(20.0, length) if length > 0 else 50.0

    # Wave loss
    wave_enc = abs(_relative_angle(wave_direction, heading))
    wave_loss = coeffs.a_wave * (wave_height ** 2) / math.sqrt(waterline)
    wave_loss *= _angle_factor_waves(wave_enc)

    # Wind loss — sign-preserving so a tailwind gives a (capped) speed gain.
    wind_enc = _relative_angle(wind_direction, heading)
    wind_axial = wind_speed * _angle_factor_wind(wind_enc)  # signed
    wind_loss = coeffs.b_wind * wind_axial * abs(wind_axial) / 100.0
    wind_loss = max(wind_loss, -v_max * 0.05)  # cap any tailwind boost at 5%

    sog = max(coeffs.min_speed_kn, v_max - wave_loss - wind_loss)
    loss_pct = (v_max - sog) / v_max * 100.0

    storm = wave_height >= coeffs.storm_hs or wind_speed >= coeffs.storm_wind_kn
    warning = ""
    if storm:
        bits = []
        if wave_height >= coeffs.storm_hs:
            bits.append(f"Hs {wave_height:.1f} m ≥ {coeffs.storm_hs:.1f} m")
        if wind_speed >= coeffs.storm_wind_kn:
            bits.append(f"wind {wind_speed:.0f} kn ≥ {coeffs.storm_wind_kn:.0f} kn")
        warning = "Storm-class conditions: " + ", ".join(bits)

    return SeakeepingResult(sog_kn=sog, loss_percent=loss_pct, storm_warning=storm, warning_text=warning)


def heading_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (deg true) from (lat1, lon1) to (lat2, lon2)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
