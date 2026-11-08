@echo off
rem Removes the UVMC machine, its snapshots and shortcuts (asks before each step).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
