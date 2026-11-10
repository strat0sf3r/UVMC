@echo off
rem Starts the UVMC helper: a tray icon that lets the panel manage the model on this PC.
rem It runs hidden; quit it from its tray icon. Starting it again opens the panel.
rem   helper -AutoStart     start it each time you log in (and now)
rem   helper -NoAutoStart   stop starting it at login
rem   helper -Stop          quit the running helper
if "%~1"=="" (
  start "" powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0helper.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0helper.ps1" %*
)
