@echo off
rem Copies the agent's run logs from UVMC into the repo's logs folder, so they survive a reset.
rem stop, reset and uninstall call this for you.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" info uvmc 2>nul | findstr /c:"Running" >nul || exit /b 0
cd /d "%~dp0.."
if not exist logs mkdir logs
"%MP%" exec uvmc -- sudo tar -czf /tmp/uvmc-logs.tgz -C /var/log/uvmc runs || exit /b 0
"%MP%" exec uvmc -- sudo chmod 644 /tmp/uvmc-logs.tgz
cd logs
"%MP%" transfer uvmc:/tmp/uvmc-logs.tgz uvmc-logs.tgz || exit /b 0
tar -xzf uvmc-logs.tgz && del uvmc-logs.tgz
echo Run logs saved to %CD%\runs
exit /b 0
