"""Voyage economics for the Marine Route Optimizer.

Computes voyage cost as the sum of:

  total_cost = fuel_cost + time_cost + storm_penalty

Where:

* ``fuel_cost = (consumption_tpd) * (days) * (fuel_price_usd_per_tonne)``
* ``time_cost = hire_usd_per_hour * total_hours``
* ``storm_penalty = storm_hours * storm_penalty_per_hour``

The optimizer (mode="economy") uses :func:`segment_cost_economy` as its
weighting function so that 2-opt minimizes voyage *cost* instead of time
or distance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Mode = Literal["time", "fuel", "safety", "economy"]


@dataclass(frozen=True)
class VesselEconomics:
    """Daily operating cost model for a vessel."""

    name: str
    fuel_consumption_tpd_at_service: float   # tonnes/day at service speed
    service_speed_kn: float                  # the speed the above figure applies to
    fuel_price_usd_per_tonne: float          # IFO/VLSFO ~ $600, MGO ~ $850
    hire_usd_per_hour: float                 # time charter rate
    storm_penalty_usd_per_hour: float        # extra cost for hours spent in storm-class weather


# Reasonable industry-typical defaults. Easy to override from the GUI.
ECONOMICS_PRESETS: dict[str, VesselEconomics] = {
    "container": VesselEconomics("Container ship", 180.0, 22.0, 650.0, 1200.0, 800.0),
    "tanker":    VesselEconomics("Oil tanker",      75.0, 15.0, 620.0,  900.0, 700.0),
    "bulker":    VesselEconomics("Bulk carrier",    35.0, 14.0, 620.0,  700.0, 500.0),
    "cruise":    VesselEconomics("Cruise ship",    120.0, 22.0, 700.0, 4000.0, 1500.0),
    "yacht":     VesselEconomics("Motor yacht",      2.5, 18.0, 850.0,  400.0, 300.0),
    "fishing":   VesselEconomics("Fishing vessel",   4.0, 10.0, 850.0,  250.0, 150.0),
    "tug":       VesselEconomics("Tug boat",         8.0, 12.0, 700.0,  500.0, 250.0),
    "generic":   VesselEconomics("Generic",         30.0, 14.0, 700.0,  600.0, 400.0),
}


def consumption_at_speed(econ: VesselEconomics, speed_kn: float) -> float:
    """Cube-law fuel scaling: consumption ∝ speed³ relative to service speed."""
    if econ.service_speed_kn <= 0 or speed_kn <= 0:
        return 0.0
    ratio = speed_kn / econ.service_speed_kn
    return econ.fuel_consumption_tpd_at_service * ratio ** 3


@dataclass(frozen=True)
class VoyageCost:
    fuel_tonnes: float
    fuel_cost_usd: float
    time_cost_usd: float
    storm_penalty_usd: float
    total_cost_usd: float
    total_hours: float
    storm_hours: float


def calculate_voyage_cost(
    distance_nm: float,
    average_sog_kn: float,
    storm_hours: float,
    econ: VesselEconomics,
) -> VoyageCost:
    if average_sog_kn <= 0:
        return VoyageCost(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, storm_hours)
    total_hours = distance_nm / average_sog_kn
    days = total_hours / 24.0
    consumption_tpd = consumption_at_speed(econ, average_sog_kn)
    fuel_tonnes = consumption_tpd * days
    fuel_cost = fuel_tonnes * econ.fuel_price_usd_per_tonne
    time_cost = econ.hire_usd_per_hour * total_hours
    storm_cost = econ.storm_penalty_usd_per_hour * storm_hours
    total = fuel_cost + time_cost + storm_cost
    return VoyageCost(
        fuel_tonnes=fuel_tonnes,
        fuel_cost_usd=fuel_cost,
        time_cost_usd=time_cost,
        storm_penalty_usd=storm_cost,
        total_cost_usd=total,
        total_hours=total_hours,
        storm_hours=storm_hours,
    )


@dataclass(frozen=True)
class ModeComparison:
    by_mode: dict[str, VoyageCost]
    recommended_mode: Mode
    savings_vs_time_usd: float


def compare_modes(
    distance_nm: float,
    sog_by_mode: dict[Mode, tuple[float, float]],   # mode -> (avg_sog_kn, storm_hours)
    econ: VesselEconomics,
) -> ModeComparison:
    """Score each mode and pick the cheapest as the recommendation."""
    results: dict[str, VoyageCost] = {}
    for mode, (sog, storm_h) in sog_by_mode.items():
        results[mode] = calculate_voyage_cost(distance_nm, sog, storm_h, econ)
    best_mode: Mode = "economy"
    best_cost = float("inf")
    for mode, cost in results.items():
        if cost.total_cost_usd < best_cost:
            best_cost = cost.total_cost_usd
            best_mode = mode  # type: ignore[assignment]
    time_cost = results.get("time", next(iter(results.values()))).total_cost_usd
    savings = max(0.0, time_cost - best_cost)
    return ModeComparison(by_mode=results, recommended_mode=best_mode, savings_vs_time_usd=savings)


# ---------------------------------------------------------------------------
# Segment-cost functions used as weight in the optimizer
# ---------------------------------------------------------------------------

STORM_MULTIPLIER = 3.0  # tripling cost for storm-class segments


def segment_cost_time(distance_nm: float, sog_kn: float, is_storm: bool) -> float:
    if sog_kn <= 0:
        return float("inf")
    cost = distance_nm / sog_kn
    return cost * (STORM_MULTIPLIER if is_storm else 1.0)


def segment_cost_fuel(distance_nm: float, sog_kn: float, is_storm: bool, econ: VesselEconomics) -> float:
    if sog_kn <= 0:
        return float("inf")
    hours = distance_nm / sog_kn
    days = hours / 24.0
    fuel = consumption_at_speed(econ, sog_kn) * days
    return fuel * (STORM_MULTIPLIER if is_storm else 1.0)


def segment_cost_safety(distance_nm: float, sog_kn: float, is_storm: bool) -> float:
    """Distance, but storms cost 10x — pushes the planner around weather."""
    if sog_kn <= 0:
        return float("inf")
    return distance_nm * (10.0 if is_storm else 1.0)


def segment_cost_economy(
    distance_nm: float, sog_kn: float, is_storm: bool, econ: VesselEconomics,
) -> float:
    if sog_kn <= 0:
        return float("inf")
    hours = distance_nm / sog_kn
    days = hours / 24.0
    fuel = consumption_at_speed(econ, sog_kn) * days
    fuel_cost = fuel * econ.fuel_price_usd_per_tonne
    time_cost = hours * econ.hire_usd_per_hour
    storm_cost = (hours if is_storm else 0.0) * econ.storm_penalty_usd_per_hour
    return fuel_cost + time_cost + storm_cost
