@echo off
title Deadlock Clipper - Update
rem Everything runs on one line so this file can safely be replaced while it runs.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\update.ps1" & echo. & pause & exit /b
