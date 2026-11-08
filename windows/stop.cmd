@echo off
rem Stops UVMC. Everything inside is kept; run logs are also copied to the repo's logs folder.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
call "%~dp0save-logs.cmd"
"%MP%" stop uvmc
