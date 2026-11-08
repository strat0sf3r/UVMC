@echo off
rem Opens a plain Linux shell in UVMC, as the agent user.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" start uvmc || goto missing
"%MP%" exec uvmc -- uvmc-agent shell
exit /b
:missing
echo UVMC is not installed, or Multipass is not running. Run install.cmd first.
pause
exit /b 1
