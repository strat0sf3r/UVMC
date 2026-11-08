@echo off
rem Collects why the plan sign-in or the Claude apps connection fails, into diagnose.txt.
rem Read-only; tokens are masked.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
pause
