@echo off
rem Sets or replaces an API key inside UVMC. Press Enter on an empty line to remove it.
rem   set-key            the Anthropic (Claude) key
rem   set-key deepseek   the key for a model profile (its "key" in config.toml)
setlocal
set "MP=multipass"
where multipass >nul 2>nul || set "MP=%ProgramFiles%\Multipass\bin\multipass.exe"
set "NAME=%~1"
if "%NAME%"=="" set "NAME=anthropic"
"%MP%" start uvmc || goto missing
powershell -NoProfile -Command "$s = Read-Host 'API key for %NAME% (Enter on its own removes it)' -AsSecureString; $k = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)); $k | & '%MP%' exec uvmc -- sudo uvmc-setkey '%NAME%'"
pause
exit /b
:missing
echo UVMC is not installed, or Multipass is not running. Run install.cmd first.
pause
exit /b 1
