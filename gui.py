"""Tkinter GUI for the Marine Route Optimizer.

Layout (top-to-bottom, left-to-right):

  Left column:
    - Waypoints table + Add/Edit/Remove/Move buttons
    - Vessel hydrodynamics frame (length, draft, type)
    - Weather source + forecast-hour slider
    - Bathymetry frame (avoid shallow, min depth)
    - Optimization mode (time / fuel / safety / economy)
    - "Fetch weather" + progress bar
    - "Optimize route"
  Right column:
    - Map canvas (route, wind arrows, color-coded by Hs)
    - Result tabs: Summary | Economy | Warnings
  Bottom:
    - Status bar + export buttons (GPX / NMEA / JSON)
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from optimizer import (
    DEMO_WAYPOINTS,
    VesselParams,
    Waypoint,
    estimate_duration_hours,
    format_duration,
    load_waypoints,
    optimize,
    optimize_route_with_weather,
    save_waypoints_csv,
)
from seakeeping import normalize_vessel_type
from economics import ECONOMICS_PRESETS
from weather_api import WeatherSample, fetch_async, get_weather_for_route
from grib_loader import load_grib_file, samples_from_grib
from export_import import export_to_gpx, export_to_json, export_to_nmea


APP_TITLE = "Marine Route Optimizer"
APP_VERSION = "2.0.0"


# ---------------------------------------------------------------------------
# Waypoint dialog (unchanged from v1)
# ---------------------------------------------------------------------------

class WaypointDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, initial: Waypoint | None = None) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Waypoint | None = None

        body = ttk.Frame(self, padding=12)
        body.grid(row=0, column=0)

        ttk.Label(body, text="Name:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(body, text="Latitude (-90..90):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(body, text="Longitude (-180..180):").grid(row=2, column=0, sticky="w", pady=4)

        self.name_var = tk.StringVar(value=initial.name if initial else "")
        self.lat_var = tk.StringVar(value=f"{initial.lat:.4f}" if initial else "")
        self.lon_var = tk.StringVar(value=f"{initial.lon:.4f}" if initial else "")

        name_entry = ttk.Entry(body, textvariable=self.name_var, width=28)
        name_entry.grid(row=0, column=1, padx=8, pady=4)
        ttk.Entry(body, textvariable=self.lat_var, width=28).grid(row=1, column=1, padx=8, pady=4)
        ttk.Entry(body, textvariable=self.lon_var, width=28).grid(row=2, column=1, padx=8, pady=4)

        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.grid(row=1, column=0, sticky="ew")
        ttk.Button(buttons, text="OK", command=self._on_ok).pack(side="right", padx=4)
        ttk.Button(buttons, text="Cancel", command=self._on_cancel).pack(side="right")

        self.bind("<Return>", lambda _e: self._on_ok())
        self.bind("<Escape>", lambda _e: self._on_cancel())
        name_entry.focus_set()

        self.grab_set()
        self.wait_visibility()
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")

    def _on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid input", "Name cannot be empty.", parent=self)
            return
        try:
            lat = float(self.lat_var.get())
            lon = float(self.lon_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Latitude and longitude must be numbers.", parent=self)
            return
        if not -90.0 <= lat <= 90.0:
            messagebox.showerror("Invalid input", "Latitude must be between -90 and 90.", parent=self)
            return
        if not -180.0 <= lon <= 180.0:
            messagebox.showerror("Invalid input", "Longitude must be between -180 and 180.", parent=self)
            return
        self.result = Waypoint(name, lat, lon)
        self.destroy()

    def _on_cancel(self) -> None:
        self.destroy()


# ---------------------------------------------------------------------------
# Map canvas with weather overlay
# ---------------------------------------------------------------------------

class RouteMap(tk.Canvas):
    PAD = 16

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, background="#0a2540", highlightthickness=1, highlightbackground="#22344a")
        self._waypoints: list[Waypoint] = []
        self._route: list[Waypoint] = []
        self._weather: list[WeatherSample] = []
        self._segment_storm: list[bool] = []
        self._segment_hs: list[float] = []
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_data(
        self,
        waypoints: list[Waypoint],
        route: list[Waypoint],
        weather: list[WeatherSample] | None = None,
        segment_storm: list[bool] | None = None,
        segment_hs: list[float] | None = None,
    ) -> None:
        self._waypoints = waypoints
        self._route = route
        self._weather = weather or []
        self._segment_storm = segment_storm or []
        self._segment_hs = segment_hs or []
        self._redraw()

    def _bounds(self) -> tuple[float, float, float, float]:
        pts = self._route or self._waypoints
        if not pts:
            return -10.0, 10.0, -10.0, 10.0
        lats = [p.lat for p in pts]
        lons = [p.lon for p in pts]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        lat_span = max(max_lat - min_lat, 2.0) * 0.15
        lon_span = max(max_lon - min_lon, 2.0) * 0.15
        return min_lat - lat_span, max_lat + lat_span, min_lon - lon_span, max_lon + lon_span

    def _project(self, lat: float, lon: float, w: int, h: int) -> tuple[float, float]:
        min_lat, max_lat, min_lon, max_lon = self._bounds()
        usable_w = max(w - 2 * self.PAD, 1)
        usable_h = max(h - 2 * self.PAD, 1)
        x = self.PAD + (lon - min_lon) / (max_lon - min_lon) * usable_w
        y = self.PAD + (max_lat - lat) / (max_lat - min_lat) * usable_h
        return x, y

    @staticmethod
    def _hs_color(hs: float, is_storm: bool) -> str:
        if is_storm or hs >= 5.0:
            return "#ff5e5e"
        if hs >= 3.0:
            return "#ffd84d"
        if hs >= 1.5:
            return "#a3e635"
        return "#7ec8ff"

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return

        for i in range(1, 6):
            x = self.PAD + (w - 2 * self.PAD) * i / 6
            self.create_line(x, self.PAD, x, h - self.PAD, fill="#16334d")
            y = self.PAD + (h - 2 * self.PAD) * i / 6
            self.create_line(self.PAD, y, w - self.PAD, y, fill="#16334d")
        self.create_rectangle(self.PAD, self.PAD, w - self.PAD, h - self.PAD, outline="#22344a")

        if not self._waypoints and not self._route:
            self.create_text(w // 2, h // 2, text="No waypoints",
                             fill="#5d7691", font=("TkDefaultFont", 11, "italic"))
            return

        # Route polyline — colored per segment by Hs/storm
        if len(self._route) >= 2:
            for i in range(len(self._route) - 1):
                x1, y1 = self._project(self._route[i].lat, self._route[i].lon, w, h)
                x2, y2 = self._project(self._route[i + 1].lat, self._route[i + 1].lon, w, h)
                hs = self._segment_hs[i] if i < len(self._segment_hs) else 0.0
                storm = self._segment_storm[i] if i < len(self._segment_storm) else False
                color = self._hs_color(hs, storm)
                self.create_line(x1, y1, x2, y2, fill=color, width=3, smooth=False)

        # Wind arrows at each waypoint (if weather loaded)
        for wp, sample in zip(self._waypoints, self._weather):
            x, y = self._project(wp.lat, wp.lon, w, h)
            self._draw_wind_arrow(x, y, sample.wind_direction_deg, sample.wind_speed_kn)

        # Waypoint dots + labels
        labeled = self._route if self._route else self._waypoints
        for idx, wp in enumerate(labeled, start=1):
            x, y = self._project(wp.lat, wp.lon, w, h)
            r = 5
            self.create_oval(x - r, y - r, x + r, y + r, fill="#ffd84d", outline="#ffffff", width=1)
            self.create_text(x + 8, y - 8, text=f"{idx}. {wp.name}", fill="#e8f0fa",
                             anchor="w", font=("TkDefaultFont", 9))

    def _draw_wind_arrow(self, x: float, y: float, direction_deg: float, speed_kn: float) -> None:
        """Draw a wind arrow originating at (x, y) pointing along the wind vector.

        The arrow shows where the wind is going *to* (i.e. opposite of from-direction).
        Length scales with speed up to 30 kn.
        """
        import math as _m

        # Wind direction is "FROM" — convert to "TO" by adding 180°.
        to_deg = (direction_deg + 180.0) % 360.0
        # Tk Y axis is downward; bearing 0° = North = up on screen.
        rad = _m.radians(to_deg)
        length = 12.0 + min(speed_kn, 30.0) * 0.6
        dx = length * _m.sin(rad)
        dy = -length * _m.cos(rad)
        x2, y2 = x + dx, y + dy
        self.create_line(x, y, x2, y2, fill="#a0d8ff", width=1, arrow="last", arrowshape=(7, 9, 3))


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(1080, 640)

        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        self._waypoints: list[Waypoint] = []
        self._optimized: list[Waypoint] = []
        self._weather: list[WeatherSample] = []
        self._last_report = None
        self._grib_path: Path | None = None
        self._fetch_thread: threading.Thread | None = None
        self._fetch_queue: queue.Queue = queue.Queue()

        self._build_menu()
        self._build_ui()
        self._refresh_table()
        self._poll_fetch_queue()

    # ---------- Menu ----------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load waypoints...", command=self._on_load, accelerator="Ctrl+O")
        file_menu.add_command(label="Save waypoints as CSV...", command=self._on_save, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Load demo dataset", command=self._on_load_demo)
        file_menu.add_command(label="Load GRIB file...", command=self._on_load_grib)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        export_menu = tk.Menu(menubar, tearoff=0)
        export_menu.add_command(label="Export route as GPX...", command=self._on_export_gpx)
        export_menu.add_command(label="Export route as NMEA-0183...", command=self._on_export_nmea)
        export_menu.add_command(label="Copy route JSON to clipboard", command=self._on_copy_json)
        menubar.add_cascade(label="Export", menu=export_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._on_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        self.bind_all("<Control-o>", lambda _e: self._on_load())
        self.bind_all("<Control-s>", lambda _e: self._on_save())

    # ---------- Layout ----------

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ===== LEFT COLUMN =====
        left = ttk.Frame(root)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)

        # Waypoints frame
        wp_frame = ttk.LabelFrame(left, text="Waypoints", padding=8)
        wp_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        wp_frame.columnconfigure(0, weight=1)
        wp_frame.rowconfigure(0, weight=1)

        cols = ("idx", "name", "lat", "lon")
        self.tree = ttk.Treeview(wp_frame, columns=cols, show="headings", selectmode="browse", height=10)
        self.tree.heading("idx", text="#")
        self.tree.heading("name", text="Name")
        self.tree.heading("lat", text="Lat")
        self.tree.heading("lon", text="Lon")
        self.tree.column("idx", width=32, anchor="e", stretch=False)
        self.tree.column("name", width=160, anchor="w")
        self.tree.column("lat", width=72, anchor="e", stretch=False)
        self.tree.column("lon", width=72, anchor="e", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self._on_edit())
        vsb = ttk.Scrollbar(wp_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        btns = ttk.Frame(wp_frame)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for i in range(4):
            btns.columnconfigure(i, weight=1)
        ttk.Button(btns, text="Add", command=self._on_add).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Edit", command=self._on_edit).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Remove", command=self._on_remove).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Clear", command=self._on_clear).grid(row=0, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Up", command=lambda: self._on_move(-1)).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Down", command=lambda: self._on_move(1)).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Load", command=self._on_load).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Save", command=self._on_save).grid(row=1, column=3, sticky="ew", padx=2, pady=2)

        # Vessel hydrodynamics frame
        v_frame = ttk.LabelFrame(left, text="Vessel hydrodynamics", padding=8)
        v_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        v_frame.columnconfigure(1, weight=1)
        ttk.Label(v_frame, text="Type:").grid(row=0, column=0, sticky="w")
        vessel_keys = list(ECONOMICS_PRESETS.keys())
        self.vessel_type_var = tk.StringVar(value="container")
        self.vessel_combo = ttk.Combobox(v_frame, textvariable=self.vessel_type_var,
                                         values=vessel_keys, state="readonly", width=14)
        self.vessel_combo.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.vessel_combo.bind("<<ComboboxSelected>>", self._on_vessel_changed)

        ttk.Label(v_frame, text="Service speed (kn):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.speed_var = tk.DoubleVar(value=22.0)
        ttk.Spinbox(v_frame, from_=1, to=60, increment=0.5,
                    textvariable=self.speed_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        ttk.Label(v_frame, text="Length (m):").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.length_var = tk.DoubleVar(value=200.0)
        ttk.Spinbox(v_frame, from_=10, to=500, increment=5,
                    textvariable=self.length_var, width=8).grid(row=2, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        ttk.Label(v_frame, text="Draft (m):").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.draft_var = tk.DoubleVar(value=10.0)
        ttk.Spinbox(v_frame, from_=0.5, to=30, increment=0.5,
                    textvariable=self.draft_var, width=8).grid(row=3, column=1, sticky="w", padx=(4, 0), pady=(4, 0))

        # Weather source frame
        w_frame = ttk.LabelFrame(left, text="Weather source", padding=8)
        w_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        w_frame.columnconfigure(1, weight=1)
        self.weather_source_var = tk.StringVar(value="climate")
        ttk.Radiobutton(w_frame, text="Climatology (offline)", variable=self.weather_source_var,
                        value="climate").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(w_frame, text="Open-Meteo Marine API", variable=self.weather_source_var,
                        value="api").grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(w_frame, text="GRIB file", variable=self.weather_source_var,
                        value="grib").grid(row=2, column=0, columnspan=2, sticky="w")
        self.grib_label_var = tk.StringVar(value="(no GRIB loaded)")
        ttk.Label(w_frame, textvariable=self.grib_label_var, foreground="#666").grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Label(w_frame, text="Forecast +N hours:").grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.forecast_hours_var = tk.IntVar(value=0)
        ttk.Scale(w_frame, from_=0, to=120, orient="horizontal",
                  variable=self.forecast_hours_var,
                  command=lambda _v: self._update_forecast_label()).grid(row=4, column=1, sticky="ew", padx=(4, 0), pady=(6, 0))
        self.forecast_label_var = tk.StringVar(value="now")
        ttk.Label(w_frame, textvariable=self.forecast_label_var).grid(row=5, column=0, columnspan=2, sticky="w")

        self.fetch_btn = ttk.Button(w_frame, text="Fetch weather for route", command=self._on_fetch_weather)
        self.fetch_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.progress = ttk.Progressbar(w_frame, orient="horizontal", mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        # Bathymetry frame
        b_frame = ttk.LabelFrame(left, text="Bathymetry", padding=8)
        b_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        b_frame.columnconfigure(1, weight=1)
        self.avoid_shallow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(b_frame, text="Avoid shallow water",
                        variable=self.avoid_shallow_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(b_frame, text="Min clearance below keel (m):").grid(row=1, column=0, sticky="w")
        self.min_clearance_var = tk.DoubleVar(value=5.0)
        ttk.Spinbox(b_frame, from_=0, to=50, increment=0.5,
                    textvariable=self.min_clearance_var, width=8).grid(row=1, column=1, sticky="w", padx=(4, 0))

        # Optimization mode
        o_frame = ttk.LabelFrame(left, text="Optimization", padding=8)
        o_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        o_frame.columnconfigure(1, weight=1)
        ttk.Label(o_frame, text="Mode:").grid(row=0, column=0, sticky="w")
        self.opt_mode_var = tk.StringVar(value="economy")
        ttk.Combobox(o_frame, textvariable=self.opt_mode_var,
                     values=["time", "fuel", "safety", "economy"], state="readonly",
                     width=12).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(o_frame, text="Return to start", variable=self.loop_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Button(o_frame, text="Optimize route", command=self._on_optimize).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # ===== RIGHT COLUMN =====
        right = ttk.Frame(root)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=3)
        right.columnconfigure(0, weight=1)

        map_frame = ttk.LabelFrame(right, text="Route map", padding=6)
        map_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        map_frame.rowconfigure(0, weight=1)
        map_frame.columnconfigure(0, weight=1)
        self.map = RouteMap(map_frame)
        self.map.grid(row=0, column=0, sticky="nsew")

        # Tabbed result panel
        nb = ttk.Notebook(right)
        nb.grid(row=1, column=0, sticky="nsew")

        # --- Summary tab: segments table ---
        sum_tab = ttk.Frame(nb)
        nb.add(sum_tab, text="Segments")
        sum_tab.rowconfigure(0, weight=1)
        sum_tab.columnconfigure(0, weight=1)
        seg_cols = ("idx", "leg", "dist", "wind", "wave", "sog", "hrs", "fuel", "cost", "storm")
        self.seg_tree = ttk.Treeview(sum_tab, columns=seg_cols, show="headings", height=10)
        headings = [("idx", "#", 32), ("leg", "Leg", 200), ("dist", "Dist NM", 70),
                    ("wind", "Wind kn", 70), ("wave", "Hs m", 60), ("sog", "SOG kn", 70),
                    ("hrs", "Hours", 60), ("fuel", "Fuel t", 60), ("cost", "Cost $", 90),
                    ("storm", "Storm?", 60)]
        for k, label, w in headings:
            self.seg_tree.heading(k, text=label)
            self.seg_tree.column(k, width=w, anchor="e" if k != "leg" else "w", stretch=(k == "leg"))
        self.seg_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(sum_tab, orient="vertical", command=self.seg_tree.yview)
        self.seg_tree.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        self.seg_tree.tag_configure("storm", background="#5a1f1f", foreground="#ffe5e5")

        # --- Economy tab ---
        econ_tab = ttk.Frame(nb)
        nb.add(econ_tab, text="Economy")
        self.econ_text = tk.Text(econ_tab, height=12, wrap="word", font=("TkFixedFont", 10))
        self.econ_text.pack(fill="both", expand=True)
        self.econ_text.configure(state="disabled")

        # --- Warnings tab ---
        warn_tab = ttk.Frame(nb)
        nb.add(warn_tab, text="Warnings")
        self.warn_text = tk.Text(warn_tab, height=12, wrap="word", font=("TkFixedFont", 10), foreground="#cc3333")
        self.warn_text.pack(fill="both", expand=True)
        self.warn_text.configure(state="disabled")

        # Status bar
        self.status_var = tk.StringVar(value="Ready. Add waypoints or load a file to begin.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken", padding=(8, 2)).pack(fill="x", side="bottom")

    # ---------- Helpers ----------

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, wp in enumerate(self._waypoints, start=1):
            self.tree.insert("", "end", values=(i, wp.name, f"{wp.lat:+.4f}", f"{wp.lon:+.4f}"))
        segment_hs = [s.wave_m for s in self._last_report.segments] if self._last_report else []
        segment_storm = [s.storm for s in self._last_report.segments] if self._last_report else []
        self.map.set_data(self._waypoints, self._optimized, self._weather, segment_storm, segment_hs)

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    def _set_econ(self, text: str) -> None:
        self.econ_text.configure(state="normal")
        self.econ_text.delete("1.0", "end")
        self.econ_text.insert("1.0", text)
        self.econ_text.configure(state="disabled")

    def _set_warn(self, lines: list[str]) -> None:
        self.warn_text.configure(state="normal")
        self.warn_text.delete("1.0", "end")
        self.warn_text.insert("1.0", "\n".join(lines) if lines else "No warnings.")
        self.warn_text.configure(state="disabled")

    def _update_forecast_label(self) -> None:
        h = self.forecast_hours_var.get()
        self.forecast_label_var.set("now" if h == 0 else f"+{h} h ({h / 24:.1f} d)")

    def _start_datetime(self) -> datetime:
        return datetime.now(tz=timezone.utc).replace(microsecond=0)

    def _vessel_params(self) -> VesselParams:
        return VesselParams(
            type_key=normalize_vessel_type(self.vessel_type_var.get()),
            v_max_kn=float(self.speed_var.get()),
            length_m=float(self.length_var.get()),
            draft_m=float(self.draft_var.get()),
        )

    # ---------- Waypoint actions ----------

    def _on_add(self) -> None:
        dlg = WaypointDialog(self, "Add waypoint")
        self.wait_window(dlg)
        if dlg.result:
            self._waypoints.append(dlg.result)
            self._weather = []
            self._optimized = []
            self._last_report = None
            self._refresh_table()
            self._set_status(f"Added '{dlg.result.name}'.")

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            self._set_status("Select a waypoint to edit.")
            return
        dlg = WaypointDialog(self, "Edit waypoint", initial=self._waypoints[idx])
        self.wait_window(dlg)
        if dlg.result:
            self._waypoints[idx] = dlg.result
            self._weather = []
            self._optimized = []
            self._last_report = None
            self._refresh_table()

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        self._waypoints.pop(idx)
        self._weather = []
        self._optimized = []
        self._last_report = None
        self._refresh_table()

    def _on_clear(self) -> None:
        if not self._waypoints:
            return
        if not messagebox.askyesno("Clear waypoints", "Remove all waypoints?"):
            return
        self._waypoints = []
        self._weather = []
        self._optimized = []
        self._last_report = None
        self._refresh_table()
        self.seg_tree.delete(*self.seg_tree.get_children())
        self._set_econ("")
        self._set_warn([])

    def _on_move(self, delta: int) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not 0 <= new_idx < len(self._waypoints):
            return
        self._waypoints[idx], self._waypoints[new_idx] = self._waypoints[new_idx], self._waypoints[idx]
        self._weather = []
        self._optimized = []
        self._last_report = None
        self._refresh_table()
        children = self.tree.get_children()
        self.tree.selection_set(children[new_idx])
        self.tree.focus(children[new_idx])

    def _on_load(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Load waypoints",
            filetypes=[("CSV files", "*.csv"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path_str:
            return
        try:
            loaded = load_waypoints(Path(path_str))
        except Exception as exc:
            messagebox.showerror("Load failed", f"Could not parse file:\n{exc}")
            return
        if not loaded:
            messagebox.showwarning("Load", "File contained no waypoints.")
            return
        self._waypoints = loaded
        self._weather = []
        self._optimized = []
        self._last_report = None
        self._refresh_table()
        self._set_status(f"Loaded {len(loaded)} waypoints from {Path(path_str).name}.")

    def _on_save(self) -> None:
        if not self._waypoints:
            messagebox.showinfo("Save", "There are no waypoints to save.")
            return
        path_str = filedialog.asksaveasfilename(
            title="Save waypoints", defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")], initialfile="waypoints.csv",
        )
        if not path_str:
            return
        try:
            save_waypoints_csv(Path(path_str), self._waypoints)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._set_status(f"Saved {len(self._waypoints)} waypoints to {Path(path_str).name}.")

    def _on_load_demo(self) -> None:
        self._waypoints = list(DEMO_WAYPOINTS)
        self._weather = []
        self._optimized = []
        self._last_report = None
        self._refresh_table()
        self._set_status(f"Loaded demo dataset ({len(self._waypoints)} ports).")

    def _on_load_grib(self) -> None:
        path_str = filedialog.askopenfilename(
            title="Load GRIB file",
            filetypes=[("GRIB files", "*.grb *.grib *.grib2 *.grb2"), ("All files", "*.*")],
        )
        if not path_str:
            return
        try:
            info = load_grib_file(path_str)
        except Exception as exc:
            messagebox.showerror("GRIB load failed", str(exc))
            return
        self._grib_path = Path(path_str)
        self.weather_source_var.set("grib")
        self.grib_label_var.set(f"GRIB: {self._grib_path.name} — {len(info.messages)} msg, {info.notice}")
        self._set_status(f"GRIB loaded: {info.notice}")

    def _on_vessel_changed(self, _event: object = None) -> None:
        key = self.vessel_type_var.get()
        preset = ECONOMICS_PRESETS.get(normalize_vessel_type(key))
        if preset is not None:
            self.speed_var.set(preset.service_speed_kn)

    # ---------- Weather ----------

    def _on_fetch_weather(self) -> None:
        if len(self._waypoints) < 1:
            messagebox.showinfo("Weather", "Add at least one waypoint first.")
            return
        if self._fetch_thread is not None and self._fetch_thread.is_alive():
            return  # already running

        source = self.weather_source_var.get()
        start = self._start_datetime()
        from datetime import timedelta
        forecast_start = start + timedelta(hours=self.forecast_hours_var.get())

        if source == "grib" and self._grib_path is not None:
            try:
                info = load_grib_file(self._grib_path)
            except Exception as exc:
                messagebox.showerror("GRIB load failed", str(exc))
                return
            samples = samples_from_grib(info, self._waypoints, forecast_start)
            self._on_weather_ready(samples, note=f"GRIB ({len(info.messages)} msg)")
            return

        use_api = (source == "api")
        speed = float(self.speed_var.get())
        self.fetch_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=len(self._waypoints))
        self._set_status("Fetching weather...")

        def progress(done: int, total: int) -> None:
            self._fetch_queue.put(("progress", done, total))

        def on_done(samples: list[WeatherSample]) -> None:
            self._fetch_queue.put(("done", samples, source))

        def on_error(exc: Exception) -> None:
            self._fetch_queue.put(("error", str(exc)))

        self._fetch_thread = fetch_async(
            self._waypoints, forecast_start, speed, use_api,
            on_done=on_done, on_error=on_error, progress_cb=progress,
        )

    def _poll_fetch_queue(self) -> None:
        try:
            while True:
                msg = self._fetch_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.progress.configure(value=msg[1], maximum=msg[2])
                elif kind == "done":
                    samples = msg[1]
                    note = msg[2]
                    self.fetch_btn.configure(state="normal")
                    self._on_weather_ready(samples, note=note)
                elif kind == "error":
                    self.fetch_btn.configure(state="normal")
                    self._set_status(f"Weather fetch failed: {msg[1]}")
                    messagebox.showerror("Weather fetch failed", msg[1])
        except queue.Empty:
            pass
        self.after(120, self._poll_fetch_queue)

    def _on_weather_ready(self, samples: list[WeatherSample], note: str) -> None:
        self._weather = samples
        self.progress.configure(value=self.progress["maximum"])
        sources = sorted({s.source for s in samples})
        self._set_status(f"Weather ready ({note}; sources: {', '.join(sources)}).")
        self._refresh_table()

    # ---------- Optimize ----------

    def _on_optimize(self) -> None:
        if len(self._waypoints) < 2:
            messagebox.showinfo("Optimize", "Add at least two waypoints first.")
            return
        try:
            speed = float(self.speed_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Optimize", "Speed must be a positive number.")
            return
        if speed <= 0:
            messagebox.showerror("Optimize", "Speed must be greater than zero.")
            return

        loop = self.loop_var.get()
        vessel = self._vessel_params()
        mode = self.opt_mode_var.get()
        clearance = float(self.min_clearance_var.get()) if self.avoid_shallow_var.get() else None

        self._set_status("Optimizing...")
        self.update_idletasks()

        # Fast path: no weather → use plain optimizer (just distance), still computes ETA
        if not self._weather:
            route, total = optimize(self._waypoints, return_to_start=loop)
            self._optimized = route
            self._last_report = None
            eta_h = estimate_duration_hours(total, speed)
            self._refresh_table()
            self._render_summary_basic(route, total, eta_h, speed, loop)
            self._set_warn([])
            self._set_status(f"Optimized (no weather): {total:,.1f} NM, ETA {format_duration(eta_h)}.")
            return

        if len(self._weather) != len(self._waypoints):
            messagebox.showwarning("Optimize",
                                   "Weather data is out of sync with waypoints — fetch weather again.")
            return

        try:
            report = optimize_route_with_weather(
                self._waypoints, vessel, self._weather,
                mode=mode, min_clearance_m=clearance, return_to_start=loop,
            )
        except Exception as exc:
            messagebox.showerror("Optimize failed", str(exc))
            return

        self._last_report = report
        self._optimized = report.route
        self._refresh_table()
        self._render_summary_full(report, speed)
        self._render_economy(report, vessel)
        self._set_warn(report.storm_warnings)
        self._set_status(
            f"[{mode}] {report.total_distance_nm:,.1f} NM, "
            f"{format_duration(report.total_hours)}, "
            f"${report.total_cost_usd:,.0f}, storm {report.storm_hours:.1f} h"
        )

    def _render_summary_basic(self, route, total_nm, eta_h, speed, loop) -> None:
        self.seg_tree.delete(*self.seg_tree.get_children())
        for i in range(len(route) - 1):
            from optimizer import haversine_nm
            d = haversine_nm(route[i], route[i + 1])
            self.seg_tree.insert("", "end", values=(
                i + 1, f"{route[i].name} → {route[i + 1].name}",
                f"{d:.0f}", "-", "-", f"{speed:.1f}",
                f"{d / max(speed, 0.1):.1f}", "-", "-", "-",
            ))
        self._set_econ(
            f"No weather data loaded — distance-only optimization.\n"
            f"Total: {total_nm:,.1f} NM ({total_nm * 1.852:,.1f} km)\n"
            f"At {speed:g} kn: {format_duration(eta_h)}\n"
            f"Return to start: {'yes' if loop else 'no'}\n\n"
            f"Click 'Fetch weather for route' to enable weather-aware optimization."
        )

    def _render_summary_full(self, report, speed) -> None:
        self.seg_tree.delete(*self.seg_tree.get_children())
        for i, seg in enumerate(report.segments):
            a = report.route[i].name
            b = report.route[i + 1].name
            tags = ("storm",) if seg.storm else ()
            self.seg_tree.insert("", "end", values=(
                i + 1, f"{a} → {b}",
                f"{seg.distance_nm:.0f}",
                f"{seg.wind_kn:.0f}",
                f"{seg.wave_m:.1f}",
                f"{seg.sog_kn:.1f}",
                f"{seg.hours:.1f}",
                f"{seg.fuel_tonnes:.1f}",
                f"{seg.cost_usd:,.0f}",
                "YES" if seg.storm else "",
            ), tags=tags)

    def _render_economy(self, report, vessel: VesselParams) -> None:
        econ = ECONOMICS_PRESETS.get(normalize_vessel_type(vessel.type_key))
        lines = [
            f"Mode:               {report.mode}",
            f"Vessel:             {vessel.type_key} ({vessel.length_m:.0f} m, draft {vessel.draft_m:.1f} m, v_max {vessel.v_max_kn:g} kn)",
            f"",
            f"Total distance:     {report.total_distance_nm:,.1f} NM ({report.total_distance_nm * 1.852:,.1f} km)",
            f"Total time:         {format_duration(report.total_hours)}",
            f"Avg SOG:            {(report.total_distance_nm / max(report.total_hours, 0.001)):.2f} kn",
            f"Total fuel:         {report.total_fuel_tonnes:,.1f} tonnes",
            f"Storm hours:        {report.storm_hours:.1f} h",
            f"",
        ]
        if econ is not None:
            fuel_cost = report.total_fuel_tonnes * econ.fuel_price_usd_per_tonne
            time_cost = report.total_hours * econ.hire_usd_per_hour
            storm_cost = report.storm_hours * econ.storm_penalty_usd_per_hour
            lines += [
                f"Fuel cost:          ${fuel_cost:>14,.0f}    ({report.total_fuel_tonnes:.1f} t × ${econ.fuel_price_usd_per_tonne:.0f}/t)",
                f"Time charter:       ${time_cost:>14,.0f}    ({report.total_hours:.1f} h × ${econ.hire_usd_per_hour:.0f}/h)",
                f"Storm penalty:      ${storm_cost:>14,.0f}    ({report.storm_hours:.1f} h × ${econ.storm_penalty_usd_per_hour:.0f}/h)",
                f"───────────────────────────────────────",
                f"TOTAL:              ${report.total_cost_usd:>14,.0f}",
            ]
        self._set_econ("\n".join(lines))

    # ---------- Exports ----------

    def _ensure_route(self):
        if not self._optimized:
            messagebox.showinfo("Export", "Run 'Optimize route' first.")
            return None
        return self._optimized

    def _eta_list(self):
        if self._last_report is None:
            return None
        from datetime import timedelta
        start = self._start_datetime()
        etas = [start]
        cumulative = 0.0
        for seg in self._last_report.segments:
            cumulative += seg.hours
            etas.append(start + timedelta(hours=cumulative))
        return etas

    def _on_export_gpx(self) -> None:
        route = self._ensure_route()
        if route is None:
            return
        path = filedialog.asksaveasfilename(title="Export GPX", defaultextension=".gpx",
                                            filetypes=[("GPX", "*.gpx")], initialfile="route.gpx")
        if not path:
            return
        try:
            export_to_gpx(path, self._waypoints, route_order=route, eta_list=self._eta_list())
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._set_status(f"Exported GPX: {Path(path).name}")

    def _on_export_nmea(self) -> None:
        route = self._ensure_route()
        if route is None:
            return
        path = filedialog.asksaveasfilename(title="Export NMEA", defaultextension=".nmea",
                                            filetypes=[("NMEA", "*.nmea *.txt")], initialfile="route.nmea")
        if not path:
            return
        try:
            export_to_nmea(path, route)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self._set_status(f"Exported NMEA: {Path(path).name}")

    def _on_copy_json(self) -> None:
        route = self._ensure_route()
        if route is None:
            return
        extras = None
        if self._last_report is not None:
            extras = {
                "mode": self._last_report.mode,
                "total_distance_nm": self._last_report.total_distance_nm,
                "total_hours": self._last_report.total_hours,
                "total_fuel_tonnes": self._last_report.total_fuel_tonnes,
                "total_cost_usd": self._last_report.total_cost_usd,
                "storm_hours": self._last_report.storm_hours,
            }
        text = export_to_json(None, route, eta_per_waypoint=self._eta_list(), extras=extras)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_status("Route JSON copied to clipboard.")

    def _on_about(self) -> None:
        messagebox.showinfo(
            "About",
            f"{APP_TITLE} v{APP_VERSION}\n\n"
            "Weather-aware route optimization with vessel-specific\n"
            "hydrodynamics, voyage economics, and bathymetric checks.\n\n"
            "Sources: Open-Meteo Marine API, climatology fallback, GRIB.\n"
            "Pure-stdlib Tk frontend. No third-party runtime deps.",
        )


def launch() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    launch()
