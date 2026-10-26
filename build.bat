@echo off
rem Double-click: builds Bukvar and the installer.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
pause
