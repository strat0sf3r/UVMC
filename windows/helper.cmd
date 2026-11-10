@echo off
rem Starts the UVMC helper: a tray icon that lets the panel manage the model on this PC.
rem It runs hidden; quit it from its tray icon. Starting it again opens the panel.
start "" powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0helper.ps1"
