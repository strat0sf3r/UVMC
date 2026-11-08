@echo off
rem Shares the folders listed in shares.txt with UVMC (read-only for the agent).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0share.ps1"
