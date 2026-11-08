@echo off
rem Factory reset: returns UVMC to the clean snapshot taken at install.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
echo This wipes everything the agent did and returns UVMC to its clean install state.
set /p "ok=Type YES to continue: "
if /i not "%ok%"=="YES" exit /b
call "%~dp0save-logs.cmd"
"%MP%" stop uvmc
"%MP%" restore uvmc.clean --destructive || goto failed
"%MP%" start uvmc
echo Done: UVMC is back to its clean state.
pause
exit /b
:failed
echo Reset failed.
pause
exit /b 1
