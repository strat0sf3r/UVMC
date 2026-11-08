@echo off
rem Starts UVMC and opens your own supervisor (agent loop with your rules and logs).
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" start uvmc || goto missing
"%MP%" exec uvmc -- uvmc %*
exit /b
:missing
echo UVMC is not installed, or Multipass is not running. Run install.cmd first.
pause
exit /b 1
