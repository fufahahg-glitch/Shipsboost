"""Exporters for GPX, NMEA-0183, and JSON.

Pure stdlib — uses :mod:`xml.etree.ElementTree` for GPX, plain string
formatting for NMEA, and :mod:`json` for the JSON variant.

GPX is consumable by virtually every chartplotter, navigation app, and GPS
tool (OpenCPN, GPSBabel, Garmin BaseCamp, etc.). NMEA-0183 ``$GPWPL`` /
``$GPRTE`` sentences feed legacy marine instrumentation.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# GPX
# ---------------------------------------------------------------------------

GPX_NS = "http://www.topografix.com/GPX/1/1"


def export_to_gpx(
    path: str | Path,
    waypoints,                       # Sequence[Waypoint] for "wpt" entries
    route_order=None,                # Sequence[Waypoint] for "rte" entries (defaults to waypoints)
    eta_list: Sequence[datetime] | None = None,
    creator: str = "MarineRouteOptimizer",
) -> Path:
    """Write a GPX 1.1 file with waypoints and a single route track."""
    if route_order is None:
        route_order = list(waypoints)

    ET.register_namespace("", GPX_NS)
    gpx = ET.Element(f"{{{GPX_NS}}}gpx", {
        "version": "1.1",
        "creator": creator,
    })
    meta = ET.SubElement(gpx, f"{{{GPX_NS}}}metadata")
    name = ET.SubElement(meta, f"{{{GPX_NS}}}name")
    name.text = "Marine Route Optimizer voyage"
    time_el = ET.SubElement(meta, f"{{{GPX_NS}}}time")
    time_el.text = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for wp in waypoints:
        wpt = ET.SubElement(gpx, f"{{{GPX_NS}}}wpt", {"lat": f"{wp.lat:.6f}", "lon": f"{wp.lon:.6f}"})
        wpt_name = ET.SubElement(wpt, f"{{{GPX_NS}}}name")
        wpt_name.text = wp.name

    rte = ET.SubElement(gpx, f"{{{GPX_NS}}}rte")
    rte_name = ET.SubElement(rte, f"{{{GPX_NS}}}name")
    rte_name.text = "Optimized voyage"
    for i, wp in enumerate(route_order):
        rtept = ET.SubElement(rte, f"{{{GPX_NS}}}rtept", {"lat": f"{wp.lat:.6f}", "lon": f"{wp.lon:.6f}"})
        name_el = ET.SubElement(rtept, f"{{{GPX_NS}}}name")
        name_el.text = wp.name
        if eta_list and i < len(eta_list) and eta_list[i] is not None:
            time_el = ET.SubElement(rtept, f"{{{GPX_NS}}}time")
            time_el.text = eta_list[i].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    out = Path(path)
    tree = ET.ElementTree(gpx)
    ET.indent(tree, space="  ")
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out


# ---------------------------------------------------------------------------
# NMEA-0183
# ---------------------------------------------------------------------------

def _nmea_checksum(body: str) -> str:
    """XOR of every character between $ and *, formatted as two hex digits."""
    chk = 0
    for ch in body:
        chk ^= ord(ch)
    return f"{chk:02X}"


def _to_nmea_lat(lat: float) -> tuple[str, str]:
    hemi = "N" if lat >= 0 else "S"
    a = abs(lat)
    deg = int(a)
    minutes = (a - deg) * 60.0
    return f"{deg:02d}{minutes:07.4f}", hemi


def _to_nmea_lon(lon: float) -> tuple[str, str]:
    hemi = "E" if lon >= 0 else "W"
    a = abs(lon)
    deg = int(a)
    minutes = (a - deg) * 60.0
    return f"{deg:03d}{minutes:07.4f}", hemi


def _sanitize_name(name: str, max_len: int = 8) -> str:
    """NMEA waypoint IDs are short ASCII; strip and truncate."""
    cleaned = "".join(ch for ch in name if ch.isalnum())[:max_len].upper()
    return cleaned or "WPT"


def export_to_nmea(path: str | Path, waypoints, route_name: str = "VOYAGE") -> Path:
    lines: list[str] = []
    ids: list[str] = []
    for wp in waypoints:
        lat_s, lat_h = _to_nmea_lat(wp.lat)
        lon_s, lon_h = _to_nmea_lon(wp.lon)
        wp_id = _sanitize_name(wp.name)
        ids.append(wp_id)
        body = f"GPWPL,{lat_s},{lat_h},{lon_s},{lon_h},{wp_id}"
        lines.append(f"${body}*{_nmea_checksum(body)}")

    rt_name = _sanitize_name(route_name, max_len=12)
    chunk = 10
    total_sentences = max(1, (len(ids) + chunk - 1) // chunk)
    for n in range(total_sentences):
        slice_ids = ids[n * chunk:(n + 1) * chunk]
        body = f"GPRTE,{total_sentences},{n + 1},c,{rt_name}," + ",".join(slice_ids)
        lines.append(f"${body}*{_nmea_checksum(body)}")

    out = Path(path)
    out.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return out


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def export_to_json(
    path: str | Path | None,
    waypoints,
    sog_per_segment: Iterable[float] | None = None,
    eta_per_waypoint: Iterable[datetime] | None = None,
    extras: dict | None = None,
) -> str:
    payload: dict = {
        "format": "marine-route-optimizer/1.0",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "waypoints": [
            {"name": wp.name, "lat": wp.lat, "lon": wp.lon} for wp in waypoints
        ],
    }
    if sog_per_segment is not None:
        payload["sog_per_segment_kn"] = list(sog_per_segment)
    if eta_per_waypoint is not None:
        payload["eta_per_waypoint"] = [dt.astimezone(timezone.utc).isoformat() for dt in eta_per_waypoint]
    if extras:
        payload["extras"] = extras
    text = json.dumps(payload, indent=2, default=str)
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text
