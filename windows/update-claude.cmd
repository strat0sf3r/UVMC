@echo off
rem Installs the latest Claude Code in UVMC (for the "my plan" models). It never updates itself.
rem The panel has the same button: Settings, Your Claude plan, Update Claude Code.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" start uvmc || goto missing
"%MP%" exec uvmc -- sudo uvmc-install-claude
pause
exit /b
:missing
echo Multipass could not start the UVMC machine (its message is just above). If UVMC is not installed yet, run install.cmd.
pause
exit /b 1
