#!/usr/bin/env bash
# Build a standalone executable using PyInstaller.
# Output: dist/marine-route-optimizer (Linux/macOS) or dist/marine-route-optimizer.exe (Windows).
set -euo pipefail

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

python3 -m PyInstaller \
  --onefile \
  --name marine-route-optimizer \
  --clean \
  main.py

echo
echo "Build complete. Artifact(s):"
ls -la dist/
