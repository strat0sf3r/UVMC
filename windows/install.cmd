@echo off
rem Installs UVMC: Hyper-V (if needed), Multipass, the machine, shortcuts.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
