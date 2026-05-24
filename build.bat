@echo off
REM Build a standalone Windows executable using PyInstaller.
REM Output: dist\marine-route-optimizer.exe

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt || goto :error

python -m PyInstaller --onefile --name marine-route-optimizer --clean main.py || goto :error

echo.
echo Build complete. Artifact:
dir dist
exit /b 0

:error
echo Build failed.
exit /b 1
