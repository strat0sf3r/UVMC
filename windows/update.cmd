@echo off
rem Pushes the supervisor code and config from this repo into UVMC and installs it.
rem Run it after editing anything in the supervisor folder.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
cd /d "%~dp0.."
"%MP%" start uvmc || goto failed
"%MP%" exec uvmc -- rm -rf /tmp/supervisor
"%MP%" transfer --recursive supervisor uvmc:/tmp/ || goto failed
"%MP%" exec uvmc -- sudo bash /tmp/supervisor/setup.sh || goto failed
exit /b 0
:failed
echo Updating the supervisor failed.
exit /b 1
