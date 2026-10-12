@echo off
rem Starts Deadlock Clipper (same as the desktop shortcut).
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Deadlock Clipper is not set up yet. Please double-click Install.bat first.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app\main.py"
