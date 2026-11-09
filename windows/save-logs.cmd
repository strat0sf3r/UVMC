@echo off
rem Copies the agent's run logs from UVMC into the repo's logs folder, so they survive a reset.
rem stop, reset and uninstall call this for you.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" info uvmc 2>nul | findstr /c:"Running" >nul || exit /b 0
cd /d "%~dp0.."
if not exist logs mkdir logs
rem Stopped after 300 s if the machine doesn't answer (see timeouts.ps1).
"%MP%" exec uvmc -- timeout -k 5 300 sudo tar -czf /tmp/uvmc-logs.tgz -C /var/log/uvmc runs
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="124" goto timedout
if "%CODE%"=="137" goto timedout
if not "%CODE%"=="0" exit /b 0
"%MP%" exec uvmc -- sudo chmod 644 /tmp/uvmc-logs.tgz
cd logs
"%MP%" transfer uvmc:/tmp/uvmc-logs.tgz uvmc-logs.tgz || exit /b 0
tar -xzf uvmc-logs.tgz && del uvmc-logs.tgz
echo Run logs saved to %CD%\runs
exit /b 0
:timedout
echo The UVMC machine did not answer within 300 seconds (packing the run logs); the command was stopped. The run logs were not saved.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". '%~dp0timeouts.ps1'; Write-TimeoutLog 'save-logs' 'sudo tar -czf /tmp/uvmc-logs.tgz -C /var/log/uvmc runs' 'stopped in the VM after 300 s'"
exit /b 0
