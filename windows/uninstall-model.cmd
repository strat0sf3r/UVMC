@echo off
rem Takes the model on this PC away: stops it, deletes llama.cpp and the model (the uvmc-local
rem folder), the login shortcut, the firewall rule (Windows asks for administrator rights) and the
rem key inside UVMC. It asks before deleting anything. UVMC itself is not touched.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-model.ps1" -Remove
pause
