# Opens the UVMC panel in your browser, signed in. Starts the machine if needed.
# Start it with panel.cmd or the "UVMC Panel" shortcut. It first shows the "Start UVMC"
# window to choose folders (skip it with "Don't ask again"; hold Shift to bring it back).
#   -ChooseFolders   always show the folder window
param([switch]$ChooseFolders)

$Name = 'uvmc'
$Port = 8080

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

$ip = ((& $mp exec $Name -- hostname -I | Out-String).Trim() -split '\s+')[0]
$token = (& $mp exec $Name -- sudo cat /etc/uvmc/panel_token | Out-String).Trim()
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
