# Runs a model on this PC's graphics card for UVMC, with llama.cpp's own server (no account,
# no subscription). Start it with local-model.cmd.
#   local-model                      set up: download llama.cpp and a model sized to your card,
#                                    open the way for the UVMC machine only, start, connect UVMC
#   local-model -Model qwen3-4b      switch model (the old file is deleted: one model at a time)
#   local-model -Update              get the newest llama.cpp
#   local-model -Status | -Start | -Stop | -Remove
#   -Dir <folder>                    where to keep it (default: uvmc-local next to the UVMC folder)
#
# The server listens only on this PC and on the network to the UVMC machine, never on your
# home network, and answers only with a key that is kept inside UVMC (out of the agent's reach).
# The model only writes text; whatever it asks for is done by the supervisor inside the VM.
param(
    [string]$Model,
    [string]$Dir,
    [switch]$Update,
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Remove,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Port = 8091
$VmName = 'uvmc'
$RuleName = 'UVMC local model'
$StateFile = Join-Path $env:LOCALAPPDATA 'UVMC\local-model.json'
$Startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'UVMC local model.lnk'

# One model per size of card. All from the Qwen team's own Hugging Face page, 4-bit (Q4_K_M).
$Models = [ordered]@{
    'qwen3-4b'  = @{ Repo = 'Qwen/Qwen3-4B-GGUF';  File = 'Qwen3-4B-Q4_K_M.gguf';  MinMiB = 0;     Label = 'Qwen3 4B (2.4 GB), basic tasks' }
    'qwen3-8b'  = @{ Repo = 'Qwen/Qwen3-8B-GGUF';  File = 'Qwen3-8B-Q4_K_M.gguf';  MinMiB = 6000;  Label = 'Qwen3 8B (4.8 GB)' }
    'qwen3-14b' = @{ Repo = 'Qwen/Qwen3-14B-GGUF'; File = 'Qwen3-14B-Q4_K_M.gguf'; MinMiB = 11000; Label = 'Qwen3 14B (8.6 GB)' }
    'qwen3-32b' = @{ Repo = 'Qwen/Qwen3-32B-GGUF'; File = 'Qwen3-32B-Q4_K_M.gguf'; MinMiB = 20000; Label = 'Qwen3 32B (18.8 GB)' }
}

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

function Load-State {
    if (Test-Path $StateFile) { return Get-Content $StateFile -Raw | ConvertFrom-Json }
    return [pscustomobject]@{}
}
function Save-State($s) {
    New-Item -ItemType Directory -Force (Split-Path $StateFile) | Out-Null
    [IO.File]::WriteAllText($StateFile, ($s | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
function Set-Field($s, $name, $value) { $s | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force }

function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}

# The address the UVMC machine reaches this PC at: its network gateway. Windows gives the
# Hyper-V network a new range at each restart, so this is looked up every time, never stored.
function Find-VmNetwork {
    $mp = Find-Multipass
    if (-not $mp) { return $null }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $mp start $VmName *> $null
    $route = (& $mp exec $VmName -- ip -4 route show default 2>$null | Out-String)
    $ErrorActionPreference = $old
    if ($route -notmatch 'via (\d+\.\d+\.\d+\.\d+)') { return $null }
    $gw = $Matches[1]
    $nic = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $gw -ErrorAction SilentlyContinue
    if (-not $nic) { return $null }
    return @{ Address = $gw; Interface = $nic.InterfaceAlias }
}

function Server-Exe($s) { Join-Path $s.dir 'llama.cpp\llama-server.exe' }
function Running { Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$($state.dir)*" } }

function Get-LlamaCpp($s) {
    # The newest llama.cpp release that has a Windows Vulkan build. Vulkan works on NVIDIA,
    # AMD (old cards too, like the RX 5000 series) and Intel, through the normal graphics driver.
    Say 'Looking up the newest llama.cpp...'
    $rels = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10' -Headers @{ 'User-Agent' = 'uvmc' }
    foreach ($r in $rels) {
        $a = $r.assets | Where-Object { $_.name -like 'llama-*-bin-win-vulkan-x64.zip' } | Select-Object -First 1
        if ($a) { break }
    }
    if (-not $a) { Fail 'No Windows build found in the latest llama.cpp releases. Try again later.' }
    if ($s.PSObject.Properties['llama'] -and $s.llama -eq $r.tag_name -and (Test-Path (Server-Exe $s))) {
        Say "llama.cpp $($r.tag_name) is already the newest." 'Green'; return
    }
    $zip = Join-Path $s.dir $a.name
    Say "Downloading llama.cpp $($r.tag_name) ($([int]($a.size / 1MB)) MB)..."
    & curl.exe -sSfL -o $zip $a.browser_download_url
    if ($LASTEXITCODE -ne 0) { Fail 'The download failed.' }
    $want = ($a.digest -replace '^sha256:', '').ToLower()
    $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if (-not $want -or $want -ne $got) { Remove-Item $zip; Fail 'The download does not match its published checksum; deleted it.' }
    $wasRunning = [bool](Running)
    if ($wasRunning) { Stop-Server }
    $dest = Join-Path $s.dir 'llama.cpp'
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Expand-Archive $zip $dest
    Remove-Item $zip
    Set-Field $s 'llama' $r.tag_name
    Say "llama.cpp $($r.tag_name) installed (checksum verified)." 'Green'
}

function Get-Card($s) {
    # Asks llama.cpp which card it sees: "Vulkan0: AMD Radeon RX 5700 XT (8176 MiB, 7382 MiB free)"
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = (& (Server-Exe $s) --list-devices 2>&1 | Out-String)
    $ErrorActionPreference = $old
    $best = $null
    foreach ($m in [regex]::Matches($out, '(?m)^\s*Vulkan\d+:\s*(.+?)\s*\((\d+) MiB')) {
        $c = @{ Name = $m.Groups[1].Value; MiB = [int]$m.Groups[2].Value }
        if (-not $best -or $c.MiB -gt $best.MiB) { $best = $c }
    }
    return $best
}

function Pick-Model($mib) {
    $pick = 'qwen3-4b'
    foreach ($k in $Models.Keys) { if ($mib -ge $Models[$k].MinMiB) { $pick = $k } }
    return $pick
}

function Get-Model($s, $name) {
    $m = $Models[$name]
    $path = Join-Path $s.dir "models\$($m.File)"
    $info = Invoke-RestMethod "https://huggingface.co/api/models/$($m.Repo)/tree/main" -Headers @{ 'User-Agent' = 'uvmc' }
    $want = (($info | Where-Object { $_.path -eq $m.File }).lfs.oid)
    if (-not $want) { Fail "$($m.File) was not found on huggingface.co/$($m.Repo)." }
    $verified = (Test-Path $path) -and $s.PSObject.Properties['model_sha'] -and $s.model_sha -eq $want
    if (-not $verified -and (Test-Path $path)) {
        # Already in the folder (or half downloaded): if it's complete and correct, keep it.
        Say 'Checking the model file already in the folder...'
        if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLower() -eq $want) { $verified = $true; Set-Field $s 'model_sha' $want }
    }
    if (-not $verified) {
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        Say "Downloading $($m.Label) from huggingface.co/$($m.Repo)... (it can be stopped and resumed)"
        & curl.exe -fL -C - -o $path "https://huggingface.co/$($m.Repo)/resolve/main/$($m.File)"
        if ($LASTEXITCODE -ne 0) { Fail 'The download stopped. Run local-model again to resume it.' }
        Say 'Checking the file...'
        if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLower() -ne $want) {
            Remove-Item $path; Fail 'The model does not match its published checksum; deleted it. Run local-model again.'
        }
        Set-Field $s 'model_sha' $want
    }
    # One model at a time: other model files go.
    Get-ChildItem (Join-Path $s.dir 'models') -Filter *.gguf | Where-Object { $_.Name -ne $m.File } | ForEach-Object {
        Say "Deleting the previous model $($_.Name)."; Remove-Item $_.FullName
    }
    Set-Field $s 'model' $name
}

function Settings-For($card) {
    # Measured on an RX 5700 XT (8 GB): flash attention falls back to the processor on AMD and
    # Intel cards through Vulkan (10x slower on long prompts), and on 8 GB cards a smaller work
    # buffer keeps the model off shared memory (18 s instead of 32 s for a 7,000-token prompt).
    $a = @('-ngl', '99', '-np', '1', '--jinja', '--reasoning-budget', '0')
    $a += @('-fa', $(if ($card.Name -match 'NVIDIA') { 'auto' } else { 'off' }))
    $a += @('-c', $(if ($card.MiB -ge 11000) { '16384' } else { '8192' }))
    if ($card.MiB -lt 11000) { $a += @('-ub', '256') }
    return $a
}

function Open-Firewall($s, $net) {
    # Windows asks for administrator rights once, for this one rule: llama-server may accept
    # connections on the network to the UVMC machine, on its port only.
    # Already there for this llama-server? (netsh, since Get-NetFirewallRule sees nothing without
    # administrator rights, which would ask for them again on every run.)
    $have = (& netsh advfirewall firewall show rule name="$RuleName" verbose 2>$null | Out-String)
    if ($have -match [regex]::Escape((Server-Exe $s))) { return }
    Say "Allowing the UVMC machine to reach the model (Windows will ask for administrator rights)..."
    $exe = (Server-Exe $s).Replace("'", "''")
    $cmd = "Remove-NetFirewallRule -DisplayName '$RuleName' -ErrorAction SilentlyContinue; " +
           "New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Action Allow -Protocol TCP " +
           "-LocalPort $Port -Program '$exe' -InterfaceAlias '$($net.Interface)' | Out-Null"
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-Command', $cmd)
    if ($p.ExitCode -ne 0) { Fail 'The firewall rule was not added (administrator rights refused?).' }
}

function Start-Server($s) {
    if (Running) { Say 'The model is already running.' 'Green'; return }
    # The network card to the UVMC machine was found during set-up; its address changes at each
    # restart of Windows, so it's read now. (No need to start the UVMC machine for this.)
    $hosts = '127.0.0.1'
    $ip = $null
    if ($s.PSObject.Properties['vm_interface']) {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $s.vm_interface -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
    }
    if ($ip) { $hosts += ",$ip" } else { Say 'The network to the UVMC machine was not found; the model starts for this PC only.' 'Yellow' }
    $card = @{ Name = $s.card; MiB = [int]$s.card_mib }
    $m = $Models[$s.model]
    $args_ = @('-m', (Join-Path $s.dir "models\$($m.File)"), '--alias', $s.model,
               '--host', $hosts, '--port', "$Port", '--api-key-file', (Join-Path $s.dir 'api_key'),
               '--log-file', (Join-Path $s.dir 'server.log')) + (Settings-For $card)
    Start-Process (Server-Exe $s) -ArgumentList $args_ -WindowStyle Hidden | Out-Null
    $key = (Get-Content (Join-Path $s.dir 'api_key') -Raw).Trim()
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep 1
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 2
            if ($h.status -eq 'ok') { Say "Running: $($m.Label) on $($s.card)." 'Green'; return }
        } catch { if (-not (Running)) { break } }
    }
    Fail "The model did not start. See $(Join-Path $s.dir 'server.log')."
}

function Stop-Server {
    $p = Running
    if ($p) { $p | Stop-Process -Force; Say 'Stopped.' } else { Say 'It was not running.' }
}

function Send-Key($mp, $value) {
    # Stores a key in UVMC with uvmc-setkey (where the agent can't read it). Piping it into
    # "multipass exec" hangs (Multipass 1.16 on Windows passes no input through), so the key goes
    # over as a file, into the ubuntu user's home (the agent users can't read it), and is deleted
    # right after.
    $name = 'uvmc-key-' + [guid]::NewGuid().ToString('N')
    $tmp = Join-Path $env:TEMP $name
    [IO.File]::WriteAllText($tmp, "$value`n")
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    Push-Location $env:TEMP   # a relative path: "multipass transfer" reads "C:..." as a machine name
    try {
        & $mp transfer $name "${VmName}:/home/ubuntu/$name" *> $null
        if ($LASTEXITCODE -ne 0) { return $false }
        & $mp exec $VmName -- sudo sh -c "uvmc-setkey local < /home/ubuntu/$name; s=`$?; rm -f /home/ubuntu/$name; exit `$s" *> $null
        return ($LASTEXITCODE -eq 0)
    } finally {
        Pop-Location
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $ErrorActionPreference = $old
    }
}

function Connect-Uvmc($s) {
    # Gives UVMC the key, through uvmc-setkey: stored where the agent can't read it.
    $mp = Find-Multipass
    if (-not $mp) { Say 'Multipass is not installed, so UVMC was not connected. Run install.cmd, then local-model again.' 'Yellow'; return }
    $ok = Send-Key $mp (Get-Content (Join-Path $s.dir 'api_key') -Raw).Trim()
    if ($ok) { Say 'UVMC connected: pick "Local model on my PC" in the panel.' 'Green' }
    else { Say 'Could not give UVMC the key. Is the UVMC machine installed and running?' 'Yellow' }
}

function Show-Status($s) {
    if (-not $s.PSObject.Properties['dir']) { Say 'Not set up yet. Run local-model.'; return }
    Say "Folder:    $($s.dir)"
    Say "llama.cpp: $($s.llama)"
    Say "Card:      $($s.card) ($($s.card_mib) MiB)"
    Say "Model:     $($Models[$s.model].Label)"
    Say "Starts with Windows: $(if (Test-Path $Startup) { 'yes' } else { 'no' })"
    if (Running) { Say "Running, port $Port." 'Green' } else { Say 'Not running (local-model -Start).' 'Yellow' }
}

# --- main -----------------------------------------------------------------------------
$state = Load-State
if ($Dir) { Set-Field $state 'dir' ([IO.Path]::GetFullPath($Dir)) }
elseif (-not $state.PSObject.Properties['dir']) {
    Set-Field $state 'dir' (Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'uvmc-local')
}

if ($Status) { Show-Status $state; exit 0 }
if ($Stop) { Stop-Server; exit 0 }
if ($Start) {
    if (-not $state.PSObject.Properties['model']) { Fail 'Not set up yet. Run local-model first.' }
    Start-Server $state; exit 0
}
if ($Remove) {
    if (-not $Yes) {
        $a = Read-Host "Delete $($state.dir) (llama.cpp and the model) and the firewall rule? [y/N]"
        if ($a -notmatch '^[yY]') { exit 0 }
    }
    Stop-Server
    Remove-Item $Startup -ErrorAction SilentlyContinue
    if ((& netsh advfirewall firewall show rule name="$RuleName" 2>$null | Out-String) -match [regex]::Escape($RuleName)) {
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden `
            -ArgumentList @('-NoProfile', '-Command', "Remove-NetFirewallRule -DisplayName '$RuleName'")
    }
    if (Test-Path $state.dir) { Remove-Item -Recurse -Force $state.dir }
    $mp = Find-Multipass
    if ($mp) { Send-Key $mp '' | Out-Null }   # an empty key removes it
    Remove-Item $StateFile -ErrorAction SilentlyContinue
    Say 'Removed.' 'Green'; exit 0
}
if ($Model -and -not $Models.Contains($Model)) { Fail "Unknown model '$Model'. Choose one of: $($Models.Keys -join ', ')." }

# Set up, or switch model, or update.
New-Item -ItemType Directory -Force $state.dir | Out-Null
Say "Folder: $($state.dir)"
if ($Update -or -not (Test-Path (Server-Exe $state))) { Get-LlamaCpp $state }

$card = Get-Card $state
if (-not $card) { Fail 'llama.cpp found no graphics card it can use. Is the graphics driver up to date?' }
Set-Field $state 'card' $card.Name; Set-Field $state 'card_mib' $card.MiB
Say "Graphics card: $($card.Name), $($card.MiB) MiB."

$name = if ($Model) { $Model } elseif ($state.PSObject.Properties['model']) { $state.model } else { Pick-Model $card.MiB }
if ($Models[$name].MinMiB -gt $card.MiB) { Say "$($Models[$name].Label) is meant for bigger cards; it will be slow on this one." 'Yellow' }
Say "Model: $($Models[$name].Label)"
$switching = $state.PSObject.Properties['model'] -and $state.model -ne $name
if ($switching) { Stop-Server }
Get-Model $state $name

$keyFile = Join-Path $state.dir 'api_key'
if (-not (Test-Path $keyFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($keyFile, (-join ($bytes | ForEach-Object { $_.ToString('x2') })))
}
Save-State $state

$net = Find-VmNetwork
if ($net) { Set-Field $state 'vm_interface' $net.Interface; Save-State $state; Open-Firewall $state $net } else { Say 'The UVMC machine was not found; it can be connected later by running local-model again.' 'Yellow' }

if (Running) { Running | Stop-Process -Force; Start-Sleep 2 }   # restart, so it listens on today's addresses
Start-Server $state

# Start with Windows (a shortcut in your Startup folder; no administrator rights needed).
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut($Startup)
$lnk.TargetPath = 'powershell.exe'
$lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Start"
$lnk.WindowStyle = 7
$lnk.Save()

if ($net) { Connect-Uvmc $state }
Say "Done. It starts with Windows; local-model -Stop stops it, local-model -Remove takes it all away."
