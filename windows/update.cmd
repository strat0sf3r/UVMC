@echo off
rem Pushes the supervisor code and config from this repo into UVMC and installs it.
rem Run it after editing anything in the supervisor folder.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
cd /d "%~dp0.."
"%MP%" start uvmc || goto failed
rem The lock (panel) keeps every program on this PC out of the VM, this one too.
"%MP%" exec uvmc -- true >nul 2>nul || goto locked
"%MP%" exec uvmc -- rm -rf /tmp/supervisor
"%MP%" transfer --recursive supervisor uvmc:/tmp/ || goto failed
rem The VM can't see which Multipass the PC has, so tell it (the panel's Settings > Updates).
rem To be replaced by a direct check once UVMC has a PC-side app (design.md, 7m).
set "MPV="
for /f "usebackq tokens=2" %%v in (`call "%MP%" version 2^>nul ^| findstr /b /c:"multipass "`) do if not defined MPV set "MPV=%%v"
"%MP%" exec uvmc -- sudo env "UVMC_PC_MULTIPASS=%MPV%" bash /tmp/supervisor/setup.sh || goto failed
exit /b 0
:locked
echo UVMC doesn't let this PC in: it's probably locked. Unlock it in the panel (or restart the VM: multipass restart uvmc), then run update again.
exit /b 1
:failed
echo Updating the supervisor failed.
exit /b 1
