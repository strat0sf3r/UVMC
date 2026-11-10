# Opens the UVMC panel in your browser, signed in, through the UVMC helper (started if needed).
# Starts the machine if needed.
# Start it with panel.cmd or the "UVMC Panel" shortcut. It first shows the "Start UVMC"
# window to choose folders (skip it with "Don't ask again"; hold Shift to bring it back).
#   -ChooseFolders   always show the folder window
param([switch]$ChooseFolders)

$Name = 'uvmc'
$Port = 8080
. (Join-Path $PSScriptRoot 'timeouts.ps1')

function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}
function Fail($text) {
    Write-Host $text -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}

$mp = Find-Multipass
if (-not $mp) { Fail 'Multipass is not installed. Run install.cmd first.' }

# Which folders the agent sees this time.
& (Join-Path $PSScriptRoot 'folders.ps1') -Force:$ChooseFolders
if ($LASTEXITCODE -ne 0) { exit 0 }  # cancelled

& $mp start $Name
if ($LASTEXITCODE -ne 0) { Fail 'Multipass could not start the UVMC machine (its message is just above). If UVMC is not installed yet, run install.cmd.' }

Write-Host 'Sharing the chosen folders...'
# A problem with sharing shouldn't keep the panel from opening.
try { & (Join-Path $PSScriptRoot 'share.ps1') -Quiet }
catch { Write-Host "Sharing folders failed: $($_.Exception.Message). The panel opens anyway." -ForegroundColor Yellow }

# Through the UVMC helper (started if it isn't running; its tray icon appears): it finds the
# machine's current address, signs you in, and gives the panel its key, so the panel can manage
# the model on this PC. If the helper doesn't answer, the panel opens the old way below.
function Test-Helper {
    try { $c = New-Object Net.Sockets.TcpClient; $ok = $c.ConnectAsync('127.0.0.1', 8092).Wait(500); $c.Close(); return $ok }
    catch { return $false }
}
$helper = Join-Path $PSScriptRoot 'helper.ps1'
if (-not (Test-Helper) -and (Test-Path $helper)) {
    Write-Host 'Starting the UVMC helper...'
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File `"$helper`""
    for ($i = 0; $i -lt 20 -and -not (Test-Helper); $i++) { Start-Sleep -Milliseconds 500 }
}
if (Test-Helper) {
    Start-Process 'http://127.0.0.1:8092/panel'
    exit 0
}
Write-Host 'The UVMC helper did not start; opening the panel without it (the model on your PC is then managed with local-model).' -ForegroundColor Yellow

# Quick lookups, stopped after 20 s if the machine doesn't answer (see timeouts.ps1).
$ip = ((& $mp exec $Name -- timeout -k 5 20 hostname -I | Out-String).Trim() -split '\s+')[0]
if (Confirm-VmTimeout 'panel' 'hostname -I' 20 $LASTEXITCODE) { Fail 'Try again in a moment.' }
$token = (& $mp exec $Name -- timeout -k 5 20 sudo cat /etc/uvmc/panel_token | Out-String).Trim()
if (Confirm-VmTimeout 'panel' 'sudo cat /etc/uvmc/panel_token' 20 $LASTEXITCODE) { Fail 'Try again in a moment.' }
if (-not $ip) { Fail 'Could not find the machine''s address.' }
if (-not $token) { Fail 'The panel is not set up in the machine yet. Run update.cmd, then try again.' }

# Wait for the panel to answer (it starts with the machine).
for ($i = 0; $i -lt 30; $i++) {
    try {
        $c = New-Object Net.Sockets.TcpClient($ip, $Port)
        $c.Close()
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}

Start-Process ("http://{0}:{1}/?token={2}" -f $ip, $Port, $token)
