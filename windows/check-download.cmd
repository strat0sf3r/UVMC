@echo off
rem Checks why downloads from GitHub fail on this PC (for the Multipass install).
rem Writes what it finds to logs\download-check.txt. Changes nothing on your PC.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs
set "OUT=logs\download-check.txt"
set "URL=https://github.com/canonical/multipass/releases/download/v1.16.4/multipass-1.16.4%%2Bwin-win64.msi"
echo Checking, this takes up to 2 minutes...
> "%OUT%" echo === %DATE% %TIME%
>>"%OUT%" ver
>>"%OUT%" echo.
>>"%OUT%" echo === Windows proxy (WinHTTP)
>>"%OUT%" netsh winhttp show proxy
>>"%OUT%" echo === Proxy for your user
>>"%OUT%" reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable 2>&1
>>"%OUT%" reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2>&1
>>"%OUT%" reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v AutoConfigURL 2>&1
>>"%OUT%" echo === Proxy environment variables
>>"%OUT%" set | findstr /i "proxy"
>>"%OUT%" echo.
>>"%OUT%" echo === Name lookups
>>"%OUT%" nslookup github.com 2>&1
>>"%OUT%" nslookup release-assets.githubusercontent.com 2>&1
>>"%OUT%" nslookup objects.githubusercontent.com 2>&1
for %%M in (normal -4 -6 --http1.1) do (
  >>"%OUT%" echo.
  >>"%OUT%" echo === curl %%M
  if "%%M"=="normal" (
    curl.exe -sS -I -L --max-time 40 -w "RESULT: %%{http_code} from %%{remote_ip} %%{url_effective}\n" "%URL%" >>"%OUT%" 2>&1
  ) else (
    curl.exe %%M -sS -I -L --max-time 40 -w "RESULT: %%{http_code} from %%{remote_ip} %%{url_effective}\n" "%URL%" >>"%OUT%" 2>&1
  )
)
>>"%OUT%" echo.
>>"%OUT%" echo === small file from the same server
curl.exe -sS -o NUL -L --max-time 40 -w "RESULT: %%{http_code} from %%{remote_ip} %%{size_download} bytes\n" "https://github.com/canonical/multipass/releases/latest" >>"%OUT%" 2>&1
echo Done. Results are in %CD%\%OUT%
pause
