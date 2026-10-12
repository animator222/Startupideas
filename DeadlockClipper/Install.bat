@echo off
title Deadlock Clipper - Setup
echo.
echo  Setting up Deadlock Clipper.
echo  This needs an internet connection and takes about 5 minutes.
echo  You only need to do this once.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1"
echo.
pause
