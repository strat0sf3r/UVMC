@echo off
rem uvmc: use UVMC from a terminal or a script. "uvmc" alone shows the commands.
rem Tip: add this folder to your PATH to type "uvmc" from anywhere.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uvmc.ps1" %*
exit /b %ERRORLEVEL%
