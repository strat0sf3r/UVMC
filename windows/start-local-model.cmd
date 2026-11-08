@echo off
rem Starts the model on this PC's graphics card (set up once with local-model.cmd).
rem Double-click it; the window closes by itself a few seconds after the model is running.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-model.ps1" -Start
if errorlevel 1 (pause & exit /b 1)
timeout /t 5 >nul 2>nul
exit /b 0
