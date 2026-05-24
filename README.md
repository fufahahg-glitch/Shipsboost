# Marine Route Optimizer AI

Computes a near-optimal voyage through a set of waypoints using great-circle
(haversine) distance. The solver uses nearest-neighbor construction followed
by 2-opt refinement — fast, deterministic, and pure-stdlib (no runtime
dependencies).

## Run from source

```bash
python3 main.py --demo
python3 main.py examples/ports.csv --loop
```

Input files can be CSV (`name,lat,lon` header optional) or JSON
(`[{"name": ..., "lat": ..., "lon": ...}, ...]`).

## Build an executable

PyInstaller produces a single-file binary per platform (`.exe` on Windows).

### Locally

```bash
# Linux / macOS
./build.sh

# Windows
build.bat
```

Output is placed in `dist/` (`marine-route-optimizer` or
`marine-route-optimizer.exe`).

### On every push (GitHub Actions)

`.github/workflows/build-exe.yml` builds Windows, Linux, and macOS binaries on
every push and pull request. Pushing a tag like `v1.0.0` additionally attaches
the binaries to a GitHub Release.

| Trigger             | Result                                             |
| ------------------- | -------------------------------------------------- |
| push / PR to `main` | Uploads each binary as a workflow artifact         |
| tag `v*`            | Attaches binaries to the matching GitHub Release   |
| `workflow_dispatch` | Manual run from the Actions tab                    |
