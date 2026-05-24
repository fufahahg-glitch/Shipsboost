@echo off
REM Build a standalone Windows GUI executable using PyInstaller.
REM Output: dist\MarineRouteOptimizer.exe (no console window)

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt || goto :error

python -m PyInstaller --onefile --windowed --name MarineRouteOptimizer --collect-submodules tkinter --clean main.py || goto :error

echo.
echo Build complete. Artifact:
dir dist
exit /b 0

:error
echo Build failed.
exit /b 1
