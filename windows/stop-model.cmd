@echo off
rem Stops the model on this PC's graphics card (start-model.cmd starts it again).
rem Double-click it; the window closes by itself a few seconds later.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-model.ps1" -Stop
if errorlevel 1 (pause & exit /b 1)
timeout /t 3 >nul 2>nul
exit /b 0
