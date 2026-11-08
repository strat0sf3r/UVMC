@echo off
rem Opens the UVMC panel in your browser, signed in. Starts the machine if needed.
rem First asks which folders the agent may see (hold Shift, or add --folders, to always ask).
set "ARGS="
if /i "%~1"=="--folders" set "ARGS=-ChooseFolders"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0panel.ps1" %ARGS%
