@echo off
rem Signs UVMC in with your Claude plan (Pro or Max), for the panel's "my plan" models.
rem A sign-in link appears: open it, approve, and paste the code back if asked.
rem The login is kept in the machine where only the supervisor can read it, not the agent.
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
"%MP%" start uvmc || goto missing
"%MP%" exec uvmc -- sudo uvmc-claude-signin
pause
exit /b
:missing
echo UVMC is not installed, or Multipass is not running. Run install.cmd first.
pause
exit /b 1
