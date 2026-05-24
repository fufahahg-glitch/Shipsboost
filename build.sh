#!/usr/bin/env bash
# Build a standalone executable using PyInstaller.
# Output:
#   Linux/macOS: dist/MarineRouteOptimizer
#   Windows:     dist\MarineRouteOptimizer.exe
set -euo pipefail

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# --windowed: no console window on Windows/macOS (we have a Tk GUI).
# --collect-submodules tkinter: belt-and-braces for the GUI bundle.
python3 -m PyInstaller \
  --onefile \
  --windowed \
  --name MarineRouteOptimizer \
  --collect-submodules tkinter \
  --clean \
  main.py

echo
echo "Build complete. Artifact(s):"
ls -la dist/
