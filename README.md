# Marine Route Optimizer

Desktop app that computes a near-optimal voyage through a set of waypoints
using great-circle (haversine) distance, nearest-neighbor seeding, and 2-opt
refinement.

The GUI is built with Tkinter (Python stdlib) so the produced `.exe` has no
external runtime dependencies.

## Features

- Add / edit / remove / reorder waypoints in a table.
- Load waypoints from CSV or JSON; save to CSV.
- Built-in demo dataset (European ports).
- Vessel-type presets with cruising speeds (container ship, tanker, yacht, ...).
- Optional return-to-start (closed loop).
- Visual route preview on an equirectangular projection.
- ETA estimate from total distance and selected speed.

## Run from source

```bash
python3 main.py              # launch the GUI
python3 main.py --cli --demo # one-shot console mode
python3 main.py --cli examples/ports.csv --loop --speed 18
```

CSV header is optional; expected columns are `name,lat,lon`. JSON format is
`[{"name": ..., "lat": ..., "lon": ...}, ...]`.

## Build the executable

PyInstaller produces a single windowed binary (no console flash) per platform.

```bash
# Linux / macOS
./build.sh

# Windows
build.bat
```

Output lands in `dist/`:

| Platform | File |
|----------|------|
| Windows  | `dist\MarineRouteOptimizer.exe` |
| macOS    | `dist/MarineRouteOptimizer` (app) |
| Linux    | `dist/MarineRouteOptimizer` |

## Build via GitHub Actions

`.github/workflows/build-exe.yml` runs on every push / PR and produces all
three platform binaries as workflow artifacts. Pushing a tag like `v1.0.0`
additionally attaches them to a GitHub Release.
