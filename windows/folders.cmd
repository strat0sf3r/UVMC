@echo off
rem Shows the "Start UVMC" folder window on its own, then shares what you chose.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0folders.ps1" -Force && powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0share.ps1"
