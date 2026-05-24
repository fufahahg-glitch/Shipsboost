"""Tkinter GUI for the Marine Route Optimizer.

Layout:
  +---------------------------------------------------------------+
  | [Waypoint table]            | [Map canvas]                    |
  | Add / Edit / Remove / Up/Dn |                                 |
  | Load / Save / Demo / Clear  |                                 |
  +-----------------------------+---------------------------------+
  | Vessel: [combobox]  Speed: [..] kn   [x] Return to start      |
  | [Optimize route]                                              |
  +---------------------------------------------------------------+
  | Result text                                                   |
  +---------------------------------------------------------------+
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from optimizer import (
    DEMO_WAYPOINTS,
    VESSEL_TYPES,
    Waypoint,
    estimate_duration_hours,
    format_duration,
    load_waypoints,
    optimize,
    save_waypoints_csv,
)


APP_TITLE = "Marine Route Optimizer"
APP_VERSION = "1.0.0"


class WaypointDialog(tk.Toplevel):
    """Modal dialog for adding or editing a single waypoint."""

    def __init__(self, parent: tk.Misc, title: str, initial: Waypoint | None = None) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Waypoint | None = None

        body = ttk.Frame(self, padding=12)
        body.grid(row=0, column=0)

        ttk.Label(body, text="Name:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(body, text="Latitude (-90 to 90):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(body, text="Longitude (-180 to 180):").grid(row=2, column=0, sticky="w", pady=4)

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
        # Center on parent
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


class RouteMap(tk.Canvas):
    """Simple equirectangular projection. Draws waypoints and the route."""

    PAD = 16

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, background="#0a2540", highlightthickness=1, highlightbackground="#22344a")
        self._waypoints: list[Waypoint] = []
        self._route: list[Waypoint] = []
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_data(self, waypoints: list[Waypoint], route: list[Waypoint]) -> None:
        self._waypoints = waypoints
        self._route = route
        self._redraw()

    def _bounds(self) -> tuple[float, float, float, float]:
        pts = self._route or self._waypoints
        if not pts:
            return -10.0, 10.0, -10.0, 10.0
        lats = [p.lat for p in pts]
        lons = [p.lon for p in pts]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        # Add a margin so points don't sit on the border.
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

    def _redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return

        # Grid
        for i in range(1, 6):
            x = self.PAD + (w - 2 * self.PAD) * i / 6
            self.create_line(x, self.PAD, x, h - self.PAD, fill="#16334d")
            y = self.PAD + (h - 2 * self.PAD) * i / 6
            self.create_line(self.PAD, y, w - self.PAD, y, fill="#16334d")
        self.create_rectangle(self.PAD, self.PAD, w - self.PAD, h - self.PAD, outline="#22344a")

        if not self._waypoints and not self._route:
            self.create_text(w // 2, h // 2, text="No waypoints", fill="#5d7691", font=("TkDefaultFont", 11, "italic"))
            return

        # Route polyline
        if len(self._route) >= 2:
            coords: list[float] = []
            for wp in self._route:
                x, y = self._project(wp.lat, wp.lon, w, h)
                coords.extend((x, y))
            self.create_line(*coords, fill="#7ec8ff", width=2, smooth=False)

        # Waypoint dots + labels. If a route is drawn, number along the route.
        labeled = self._route if self._route else self._waypoints
        for idx, wp in enumerate(labeled, start=1):
            x, y = self._project(wp.lat, wp.lon, w, h)
            r = 5
            self.create_oval(x - r, y - r, x + r, y + r, fill="#ffd84d", outline="#ffffff", width=1)
            self.create_text(x + 8, y - 8, text=f"{idx}. {wp.name}", fill="#e8f0fa", anchor="w", font=("TkDefaultFont", 9))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("1100x720")
        self.minsize(900, 560)

        try:
            ttk.Style(self).theme_use("clam")
        except tk.TclError:
            pass

        self._waypoints: list[Waypoint] = []
        self._optimized: list[Waypoint] = []

        self._build_menu()
        self._build_ui()
        self._refresh_table()

    # ---------- Layout ----------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load waypoints...", command=self._on_load, accelerator="Ctrl+O")
        file_menu.add_command(label="Save waypoints as CSV...", command=self._on_save, accelerator="Ctrl+S")
        file_menu.add_separator()
        file_menu.add_command(label="Load demo dataset", command=self._on_load_demo)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._on_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        self.bind_all("<Control-o>", lambda _e: self._on_load())
        self.bind_all("<Control-s>", lambda _e: self._on_save())

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(1, weight=1)

        # --- Left column: waypoint table & buttons ---
        left = ttk.LabelFrame(root, text="Waypoints", padding=8)
        left.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        cols = ("idx", "name", "lat", "lon")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=14)
        self.tree.heading("idx", text="#")
        self.tree.heading("name", text="Name")
        self.tree.heading("lat", text="Lat")
        self.tree.heading("lon", text="Lon")
        self.tree.column("idx", width=36, anchor="e", stretch=False)
        self.tree.column("name", width=180, anchor="w")
        self.tree.column("lat", width=90, anchor="e", stretch=False)
        self.tree.column("lon", width=90, anchor="e", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self._on_edit())

        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        btns = ttk.Frame(left)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for i in range(4):
            btns.columnconfigure(i, weight=1)
        ttk.Button(btns, text="Add", command=self._on_add).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Edit", command=self._on_edit).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Remove", command=self._on_remove).grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Clear", command=self._on_clear).grid(row=0, column=3, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Move up", command=lambda: self._on_move(-1)).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Move down", command=lambda: self._on_move(1)).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Load file...", command=self._on_load).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        ttk.Button(btns, text="Save CSV...", command=self._on_save).grid(row=1, column=3, sticky="ew", padx=2, pady=2)

        # --- Right column: map ---
        right = ttk.LabelFrame(root, text="Route map", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.map = RouteMap(right)
        self.map.grid(row=0, column=0, sticky="nsew")

        # --- Controls row (under map) ---
        controls = ttk.LabelFrame(root, text="Voyage parameters", padding=8)
        controls.grid(row=1, column=1, sticky="nsew", pady=(8, 0))
        controls.columnconfigure(5, weight=1)

        ttk.Label(controls, text="Vessel:").grid(row=0, column=0, sticky="w")
        vessel_names = [v.name for v in VESSEL_TYPES]
        self.vessel_var = tk.StringVar(value=vessel_names[0])
        self.vessel_combo = ttk.Combobox(controls, textvariable=self.vessel_var, values=vessel_names, state="readonly", width=18)
        self.vessel_combo.grid(row=0, column=1, padx=(6, 12))
        self.vessel_combo.bind("<<ComboboxSelected>>", self._on_vessel_changed)

        ttk.Label(controls, text="Speed (kn):").grid(row=0, column=2, sticky="w")
        self.speed_var = tk.DoubleVar(value=VESSEL_TYPES[0].cruise_knots)
        speed_entry = ttk.Spinbox(controls, from_=1, to=60, increment=0.5, textvariable=self.speed_var, width=8)
        speed_entry.grid(row=0, column=3, padx=(6, 12))

        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Return to start", variable=self.loop_var).grid(row=0, column=4, padx=(0, 12))

        ttk.Button(controls, text="Optimize route", command=self._on_optimize).grid(row=0, column=5, sticky="e")

        # --- Result panel ---
        result_frame = ttk.LabelFrame(self, text="Result", padding=8)
        result_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.result_text = tk.Text(result_frame, height=8, wrap="none", font=("TkFixedFont", 10))
        self.result_text.pack(fill="x")
        self.result_text.configure(state="disabled")

        # --- Status bar ---
        self.status_var = tk.StringVar(value="Ready. Add waypoints or load a file to begin.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken", padding=(8, 2)).pack(fill="x", side="bottom")

    # ---------- Helpers ----------

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, wp in enumerate(self._waypoints, start=1):
            self.tree.insert("", "end", values=(i, wp.name, f"{wp.lat:+.4f}", f"{wp.lon:+.4f}"))
        self.map.set_data(self._waypoints, self._optimized)

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.index(sel[0])

    def _set_result(self, lines: list[str]) -> None:
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.insert("1.0", "\n".join(lines))
        self.result_text.configure(state="disabled")

    # ---------- Actions ----------

    def _on_add(self) -> None:
        dlg = WaypointDialog(self, "Add waypoint")
        self.wait_window(dlg)
        if dlg.result:
            self._waypoints.append(dlg.result)
            self._optimized = []
            self._refresh_table()
            self._set_status(f"Added '{dlg.result.name}'. {len(self._waypoints)} waypoints total.")

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            self._set_status("Select a waypoint to edit.")
            return
        dlg = WaypointDialog(self, "Edit waypoint", initial=self._waypoints[idx])
        self.wait_window(dlg)
        if dlg.result:
            self._waypoints[idx] = dlg.result
            self._optimized = []
            self._refresh_table()
            self._set_status(f"Updated '{dlg.result.name}'.")

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx is None:
            self._set_status("Select a waypoint to remove.")
            return
        removed = self._waypoints.pop(idx)
        self._optimized = []
        self._refresh_table()
        self._set_status(f"Removed '{removed.name}'.")

    def _on_clear(self) -> None:
        if not self._waypoints:
            return
        if not messagebox.askyesno("Clear waypoints", "Remove all waypoints?"):
            return
        self._waypoints = []
        self._optimized = []
        self._refresh_table()
        self._set_result([])
        self._set_status("Cleared.")

    def _on_move(self, delta: int) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not 0 <= new_idx < len(self._waypoints):
            return
        self._waypoints[idx], self._waypoints[new_idx] = self._waypoints[new_idx], self._waypoints[idx]
        self._optimized = []
        self._refresh_table()
        # Re-select the moved item
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
        self._optimized = []
        self._refresh_table()
        self._set_status(f"Loaded {len(loaded)} waypoints from {Path(path_str).name}.")

    def _on_save(self) -> None:
        if not self._waypoints:
            messagebox.showinfo("Save", "There are no waypoints to save.")
            return
        path_str = filedialog.asksaveasfilename(
            title="Save waypoints",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile="waypoints.csv",
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
        self._optimized = []
        self._refresh_table()
        self._set_status(f"Loaded demo dataset ({len(self._waypoints)} ports).")

    def _on_vessel_changed(self, _event: object = None) -> None:
        name = self.vessel_var.get()
        for v in VESSEL_TYPES:
            if v.name == name:
                self.speed_var.set(v.cruise_knots)
                break

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
        self._set_status("Optimizing...")
        self.update_idletasks()
        route, total = optimize(self._waypoints, return_to_start=loop)
        self._optimized = route
        self._refresh_table()

        eta_h = estimate_duration_hours(total, speed)
        lines = [
            f"Vessel: {self.vessel_var.get()}    Speed: {speed:g} kn    Return to start: {'yes' if loop else 'no'}",
            f"Total distance: {total:,.1f} NM ({total * 1.852:,.1f} km)",
            f"Estimated time:  {format_duration(eta_h)}",
            "",
            "Optimized order:",
        ]
        for i, wp in enumerate(route, start=1):
            lines.append(f"  {i:>3}. {wp.name:<24} ({wp.lat:+.4f}, {wp.lon:+.4f})")
        self._set_result(lines)
        self._set_status(f"Optimized: {total:,.1f} NM, ETA {format_duration(eta_h)}.")

    def _on_about(self) -> None:
        messagebox.showinfo(
            "About",
            f"{APP_TITLE} v{APP_VERSION}\n\n"
            "Computes near-optimal voyages through user-supplied waypoints "
            "using great-circle distance and 2-opt refinement.\n\n"
            "Pure Python / Tkinter. No internet required.",
        )


def launch() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    launch()
