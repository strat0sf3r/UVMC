# Runs models on this PC's graphics card for UVMC, with llama.cpp's own server (no account,
# no subscription). Start it with local-model.cmd.
#   local-model                      set up: download llama.cpp and a model sized to your card,
#                                    open the way for the UVMC machine only, start, connect UVMC
#   local-model -List                the models you can add, by model and size, and which fit
#   local-model -Add gemma4-e4b      download another model (several can be kept; the panel
#                                    picks which one UVMC uses)
#   local-model -AddFile my.gguf [-Name mine]   use a GGUF file of your own (not checked by UVMC)
#   local-model -Delete gemma4-e4b   delete a model's file (your own files are only forgotten)
#   local-model -Test [model]        check a model with UVMC's tools: tool call right? how fast?
#   local-model -Update              get the newest llama.cpp
#   local-model -Status | -Start | -Stop | -Remove
#   local-model -AutoStart           start it each time you log in (off unless you ask)
#   local-model -NoAutoStart         stop starting it at login
#   local-model -Logs on|off         the server's normal log (off: warnings and errors only,
#                                    which never hold what you and UVMC write; on: for testing)
#   -Dir <folder>                    where to keep it (default: uvmc-local next to the UVMC folder)
#
# The server listens only on this PC and on the network to the UVMC machine, never on your
# home network, and answers only with a key that is kept inside UVMC (out of the agent's reach).
# The model only writes text; whatever it asks for is done by the supervisor inside the VM.
# It runs in llama.cpp's router mode: every downloaded model is listed, and the one a request
# names is loaded (one at a time on the card).
[CmdletBinding(PositionalBinding = $false)]   # "-Test qwen3.5-9b": the name goes to $Rest, not to -Model
param(
    [string]$Model,
    [string]$Add,
    [string]$AddFile,
    [string]$Name,
    [string]$Delete,
    [string]$Dir,
    [switch]$List,
    [switch]$Test,
    [switch]$Update,
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Remove,
    [switch]$AutoStart,
    [switch]$NoAutoStart,
    [switch]$Yes,
    [ValidateSet('on', 'off')] [string]$Logs,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'timeouts.ps1')
$Port = 8091
$VmName = 'uvmc'
$RuleName = 'UVMC local model'
$OutRuleName = 'UVMC local model (no outgoing)'
$StateFile = Join-Path $env:LOCALAPPDATA 'UVMC\local-model.json'
$Startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'UVMC local model.lnk'

# The models on offer, by model and size, with what each is good and bad at: one list for this script,
# unix/local-model.sh and the panel. Every file's SHA-256 is pinned there. $Models has every size by
# its name, with its model's description.
$CatalogFile = Join-Path (Split-Path $PSScriptRoot) 'supervisor\uvmc\local_models.json'
if (-not (Test-Path $CatalogFile)) { Write-Host "The list of models ($CatalogFile) is missing." -ForegroundColor Red; exit 1 }
$Catalog = [IO.File]::ReadAllText($CatalogFile) | ConvertFrom-Json
$Models = [ordered]@{}
foreach ($f in $Catalog.families) {
    foreach ($sz in $f.sizes) {
        $o = $sz.PSObject.Copy()
        foreach ($k in 'maker', 'kind', 'by', 'recommended', 'about', 'good', 'weak') { $o | Add-Member -NotePropertyName $k -NotePropertyValue $f.$k -Force }
        $o | Add-Member -NotePropertyName 'family' -NotePropertyValue $f.label -Force
        $o | Add-Member -NotePropertyName 'family_id' -NotePropertyValue $f.id -Force
        $Models[$sz.id] = $o
    }
}

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Fail($text) { Write-Host $text -ForegroundColor Red; exit 1 }

function Own($s) {
    # Your own GGUF files (-AddFile): name -> path. Not on the list, not checked by UVMC.
    $own = [ordered]@{}
    if ($s.PSObject.Properties['own']) { foreach ($p in $s.own.PSObject.Properties) { $own[$p.Name] = $p.Value } }
    return $own
}
function Label($name) { if ($Models.Contains($name)) { $Models[$name].label } else { "$name (your own)" } }
function Fit($m, $mib) {
    # How a size suits the card: fits, tight (the file fits, little room for the conversation), too big.
    if (-not $mib) { return '' }
    if ($mib -ge 1000 * $m.card_gb) { return 'fits' }
    if ($mib -ge 1000 * $m.size_gb + 500) { return 'tight' }
    return 'too big'
}

function Load-State {
    if (Test-Path $StateFile) { return Get-Content $StateFile -Raw | ConvertFrom-Json }
    return [pscustomobject]@{}
}
function Save-State($s) {
    New-Item -ItemType Directory -Force (Split-Path $StateFile) | Out-Null
    [IO.File]::WriteAllText($StateFile, ($s | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
function Set-Field($s, $name, $value) { $s | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force }
function Installed($s) {
    $own = Own $s
    $list = if ($s.PSObject.Properties['models']) { @($s.models | Where-Object { $Models.Contains($_) }) } else { @() }
    return @($list) + @($own.Keys)
}

function Source-Line($m) {
    switch ($m.kind) {
        'official'   { "from $($m.by)'s own page" }
        'quantized'  { "$($m.maker)'s model, converted to GGUF by $($m.by)" }
        'uncensored' { "UNOFFICIAL: changed by $($m.by), outside $($m.maker)" }
    }
}

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
    $route = (& $mp exec $VmName -- timeout -k 5 20 ip -4 route show default 2>$null | Out-String)
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if (Confirm-VmTimeout 'local-model' 'ip -4 route show default' 20 $code) { return $null }
    if ($route -notmatch 'via (\d+\.\d+\.\d+\.\d+)') { return $null }
    $gw = $Matches[1]
    $nic = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $gw -ErrorAction SilentlyContinue
    if (-not $nic) { return $null }
    return @{ Address = $gw; Interface = $nic.InterfaceAlias }
}

function Server-Exe($s) { Join-Path $s.dir 'llama.cpp\llama-server.exe' }
function Preset-File($s) { Join-Path $s.dir 'models.ini' }
function Model-Path($s, $name) {
    $own = Own $s
    if ($own.Contains($name)) { return $own[$name] }
    Join-Path $s.dir "models\$($Models[$name].file)"
}
# The router and the model it has loaded are both llama-server processes from this folder.
function Running { Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$($state.dir)*" } }
function Api-Key($s) { (Get-Content (Join-Path $s.dir 'api_key') -Raw).Trim() }

# Downloads with curl. In a window, curl draws its own progress bar. Run by the UVMC helper
# (UVMC_HELPER=1, no window), it prints "Progress: 37% (2.1 of 5.6 GB)" lines instead, which the
# helper passes to the panel. $Total is the expected size in bytes (an estimate is fine).
function Download-File([string]$Url, [string]$Path, [double]$Total, [switch]$Resume, [switch]$Quiet) {
    $a = @('-fL')
    if ($Resume) { $a += @('-C', '-') }
    if (-not $env:UVMC_HELPER) {
        if ($Quiet) { $a += '-sS' }
        & curl.exe @a -o $Path $Url
        return ($LASTEXITCODE -eq 0)
    }
    $a += @('-sS', '-o', $Path, $Url)
    $psi = New-Object Diagnostics.ProcessStartInfo 'curl.exe', (($a | ForEach-Object { Format-NativeArg $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $last = -1
    while (-not $p.WaitForExit(2000)) {
        $have = if (Test-Path $Path) { (Get-Item $Path).Length } else { 0 }
        if ($Total -gt 0) {
            $pct = [Math]::Min(99, [int](100 * $have / $Total))
            if ($pct -ne $last) {
                $last = $pct
                $unit = if ($Total -ge 1GB) { @(1GB, 'GB') } else { @(1MB, 'MB') }
                Write-Host ('Progress: {0}% ({1:n1} of {2:n1} {3})' -f $pct, ($have / $unit[0]), ($Total / $unit[0]), $unit[1])
            }
        }
    }
    if ($p.ExitCode -eq 0) { Write-Host 'Progress: 100%' }
    return ($p.ExitCode -eq 0)
}

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
    if (-not (Download-File $a.browser_download_url $zip $a.size -Quiet)) { Fail 'The download failed.' }
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
    # The default model's largest size that fits the card (its smallest if none does).
    $sizes = @($Catalog.families | Where-Object { $_.id -eq $Catalog.default } | ForEach-Object { $_.sizes })
    $pick = $sizes[0].id
    foreach ($sz in $sizes) { if ($mib -ge 1000 * $sz.card_gb) { $pick = $sz.id } }
    return $pick
}

function Get-Model($s, $name) {
    # Downloads a model, checked against the SHA-256 pinned in local_models.json.
    $m = $Models[$name]
    $path = Model-Path $s $name
    $verified = @{}
    if ($s.PSObject.Properties['verified']) { foreach ($p in $s.verified.PSObject.Properties) { $verified[$p.Name] = $p.Value } }
    $ok = (Test-Path $path) -and $verified[$m.file] -eq $m.sha256
    if (-not $ok -and (Test-Path $path)) {
        # Already in the folder (or half downloaded): if it's complete and correct, keep it.
        Say "Checking $($m.file), already in the folder..."
        $ok = (Get-FileHash $path -Algorithm SHA256).Hash.ToLower() -eq $m.sha256
    }
    if (-not $ok) {
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        if ($m.kind -eq 'uncensored') { Say "$($m.label) is $(Source-Line $m). UVMC pinned the file it checked; a changed file is refused." 'Yellow' }
        Say "Downloading $($m.label) ($($m.size_gb) GB) from huggingface.co/$($m.repo)... (it can be stopped and resumed)"
        $ok = Download-File "https://huggingface.co/$($m.repo)/resolve/main/$($m.file)" $path ([double]$m.size_gb * 1GB) -Resume
        if (-not $ok) { Fail "The download stopped. Run local-model -Add $name again to resume it." }
        Say 'Checking the file...'
        if ((Get-FileHash $path -Algorithm SHA256).Hash.ToLower() -ne $m.sha256) {
            Remove-Item $path
            Fail ("$($m.file) does not match the checksum UVMC pinned; deleted it. Try again; if it happens again, " +
                  'the file was changed on Hugging Face and UVMC has to check it again before using it.')
        }
    }
    $verified[$m.file] = $m.sha256
    Set-Field $s 'verified' ([pscustomobject]$verified)
    $list = if ($s.PSObject.Properties['models']) { @($s.models | Where-Object { $Models.Contains($_) }) } else { @() }
    if ($list -notcontains $name) { $list += $name }
    Set-Field $s 'models' $list
}

function Forget-OldModel($s) {
    # Before 0.16 one model was kept (state "model"). Carry it over if it's still offered.
    if (-not $s.PSObject.Properties['model']) { return }
    $old = $s.model
    $s.PSObject.Properties.Remove('model')
    if ($s.PSObject.Properties['model_sha']) { $s.PSObject.Properties.Remove('model_sha') }
    if ($Models.Contains($old) -and (Test-Path (Model-Path $s $old))) {
        $list = @($s.models | Where-Object { $Models.Contains($_) }); if ($list -notcontains $old) { $list += $old }
        Set-Field $s 'models' $list
    }
}

function Preset-Settings($card) {
    # Measured on an RX 5700 XT (8 GB): flash attention falls back to the processor on AMD and
    # Intel cards through Vulkan (10x slower on long prompts), and on 8 GB cards a smaller work
    # buffer keeps the model off shared memory (18 s instead of 32 s for a 7,000-token prompt).
    $a = @('n-gpu-layers = 99', 'parallel = 1', 'jinja = true', 'reasoning-budget = 0')
    $a += "flash-attn = $(if ($card.Name -match 'NVIDIA') { 'auto' } else { 'off' })"
    $a += "ctx-size = $(if ($card.MiB -ge 11000) { '16384' } else { '8192' })"
    if ($card.MiB -lt 11000) { $a += 'ubatch-size = 256' }
    return $a
}

function Write-Preset($s) {
    # llama.cpp's router reads this when it starts: the settings for every model ([*]), then one
    # section per downloaded model. The section name is the name UVMC asks for.
    $card = @{ Name = $s.card; MiB = [int]$s.card_mib }
    $lines = @('; Written by local-model. Changes are lost the next time it runs.', 'version = 1', '', '[*]') + (Preset-Settings $card)
    foreach ($name in (Installed $s)) {
        $lines += ''; $lines += "[$name]"; $lines += "model = $(Model-Path $s $name)"
    }
    [IO.File]::WriteAllText((Preset-File $s), (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding $false))
}

function Open-Firewall($s, $net) {
    # Windows asks for administrator rights once, for these two rules: llama-server may accept
    # connections on the network to the UVMC machine, on its port only, and may not open any
    # connection itself. (It never needs to: answers to the UVMC machine go back on the connection
    # it opened, and downloads are done by this script. The firewall doesn't filter 127.0.0.1,
    # which the router uses to reach the model it loaded.)
    # Already there for this llama-server? (netsh, since Get-NetFirewallRule sees nothing without
    # administrator rights, which would ask for them again on every run.)
    $exeRx = [regex]::Escape((Server-Exe $s))
    $haveIn = (& netsh advfirewall firewall show rule name="$RuleName" verbose 2>$null | Out-String)
    $haveOut = (& netsh advfirewall firewall show rule name="$OutRuleName" verbose 2>$null | Out-String)
    if ($haveIn -match $exeRx -and $haveOut -match $exeRx) { return }
    Say "Allowing the UVMC machine to reach the model, and nothing else (Windows will ask for administrator rights)..."
    $exe = (Server-Exe $s).Replace("'", "''")
    $cmd = "Remove-NetFirewallRule -DisplayName '$RuleName' -ErrorAction SilentlyContinue; " +
           "Remove-NetFirewallRule -DisplayName '$OutRuleName' -ErrorAction SilentlyContinue; " +
           "New-NetFirewallRule -DisplayName '$RuleName' -Direction Inbound -Action Allow -Protocol TCP " +
           "-LocalPort $Port -Program '$exe' -InterfaceAlias '$($net.Interface)' | Out-Null; " +
           "New-NetFirewallRule -DisplayName '$OutRuleName' -Direction Outbound -Action Block " +
           "-Program '$exe' | Out-Null"
    $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
        -ArgumentList @('-NoProfile', '-Command', $cmd)
    if ($p.ExitCode -ne 0) { Fail 'The firewall rule was not added (administrator rights refused?).' }
}

# What the server logs. Off (the default): warnings and errors only, and no /slots endpoint, so
# nothing of the conversations is written or shown (UVMC's private conversations count on it).
# llama.cpp writes prompts only with --log-prompts-dir or -v, which UVMC never passes.
function Log-Args($s) {
    if ($s.PSObject.Properties['logs'] -and $s.logs) { return @('--log-verbosity', '3') }
    return @('--log-verbosity', '2', '--no-slots')
}

function Start-Server($s) {
    if (Running) { Say 'The model server is already running.' 'Green'; return }
    if (-not (Installed $s)) { Fail 'No model downloaded yet. Run local-model, or local-model -Add <model> (local-model -List shows them).' }
    if (-not (Test-Path (Preset-File $s))) { Write-Preset $s }
    # The network card to the UVMC machine was found during set-up; its address changes at each
    # restart of Windows, so it's read now. (No need to start the UVMC machine for this.)
    $hosts = '127.0.0.1'
    $ip = $null
    if ($s.PSObject.Properties['vm_interface']) {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $s.vm_interface -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
    }
    if ($ip) { $hosts += ",$ip" } else { Say 'The network to the UVMC machine was not found; the model starts for this PC only.' 'Yellow' }
    # Router mode: no model on the command line. --models-max 1: one model on the card at a time;
    # asking for another unloads the first.
    $args_ = @('--models-preset', (Preset-File $s), '--models-max', '1',
               '--host', $hosts, '--port', "$Port", '--api-key-file', (Join-Path $s.dir 'api_key'),
               '--log-file', (Join-Path $s.dir 'server.log')) + (Log-Args $s)
    Start-Process (Server-Exe $s) -ArgumentList $args_ -WindowStyle Hidden | Out-Null
    $key = Api-Key $s
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep 1
        try {
            $r = Invoke-RestMethod "http://127.0.0.1:$Port/models" -Headers @{ Authorization = "Bearer $key" } -TimeoutSec 2
            if ($r.data) {
                Say "Running on $($s.card), with: $((@($r.data | ForEach-Object { $_.id })) -join ', ')." 'Green'
                Say 'A model is loaded on the card the first time UVMC asks for it (a few seconds).'
                return
            }
        } catch { if (-not (Running)) { break } }
    }
    Fail "The model server did not start. See $(Join-Path $s.dir 'server.log')."
}

function Stop-Server {
    $p = Running
    if ($p) {
        # The router and the model it loaded; the router may still be ending the model when it is
        # stopped itself, so wait until no llama-server from this folder is left (else a restart
        # right after would find one and think it's running).
        for ($i = 0; $i -lt 30 -and $p; $i++) {
            $p | Stop-Process -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
            $p = Running
        }
        if ($p) { Fail 'llama-server did not stop. Close it in Task Manager and try again.' }
        Say 'Stopped.'
    } else { Say 'It was not running.' }
}

function Restart-IfRunning($s) {
    # The router reads models.ini when it starts.
    if (Running) { Stop-Server; Start-Server $s }
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
        & $mp exec $VmName -- timeout -k 5 20 sudo sh -c "uvmc-setkey local < /home/ubuntu/$name; s=`$?; rm -f /home/ubuntu/$name; exit `$s" *> $null
        $code = $LASTEXITCODE
        if (Confirm-VmTimeout 'local-model' 'uvmc-setkey local' 20 $code) { return $false }
        return ($code -eq 0)
    } finally {
        Pop-Location
        Remove-Item $tmp -ErrorAction SilentlyContinue
        $ErrorActionPreference = $old
    }
}

function Send-Hardware($mp, $s) {
    # Tells UVMC the card this script found (name and memory), so the panel can say which sizes fit.
    # Same way as the key: a file over "multipass transfer", put in place, deleted.
    if (-not $s.PSObject.Properties['card_mib']) { return }
    $name = 'uvmc-hw-' + [guid]::NewGuid().ToString('N')
    $tmp = Join-Path $env:TEMP $name
    $json = @{ kind = 'card'; name = [string]$s.card; mib = [int]$s.card_mib } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding $false))
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    Push-Location $env:TEMP
    try {
        & $mp transfer $name "${VmName}:/home/ubuntu/$name" *> $null
        if ($LASTEXITCODE -ne 0) { return }
        & $mp exec $VmName -- timeout -k 5 20 sudo sh -c "install -m 644 /home/ubuntu/$name /etc/uvmc/pc-hardware.json; s=`$?; rm -f /home/ubuntu/$name; exit `$s" *> $null
        Confirm-VmTimeout 'local-model' 'install pc-hardware.json' 20 $LASTEXITCODE | Out-Null
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
    $ok = Send-Key $mp (Api-Key $s)
    if ($ok) { Send-Hardware $mp $s }
    if ($ok) { Say 'UVMC connected: pick "Local model on my PC" in the panel, and which model it uses under Settings > Models.' 'Green' }
    else { Say 'Could not give UVMC the key. Is the UVMC machine installed and running?' 'Yellow' }
}

function Set-AutoStart([bool]$on) {
    # Only when you ask: a shortcut in your Startup folder (no administrator rights needed).
    if (-not $on) {
        if (Test-Path $Startup) { Remove-Item $Startup; Say 'It no longer starts when you log in.' }
        else { Say 'It was not starting at login.' }
        return
    }
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($Startup)
    $lnk.TargetPath = 'powershell.exe'
    $lnk.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Start"
    $lnk.WindowStyle = 7
    $lnk.Save()
    Say 'It now starts each time you log in (local-model -NoAutoStart turns that off).' 'Green'
}

function Show-List($s) {
    $have = @(Installed $s)
    $mib = if ($s.PSObject.Properties['card_mib']) { [int]$s.card_mib } else { 0 }
    if ($mib) { Say "Your card: $($s.card), $mib MiB. [x] = downloaded; each size says whether it fits." }
    else { Say '[x] = downloaded. (Run local-model once so it knows your card and says which sizes fit.)' }
    $colors = @{ 'fits' = 'Green'; 'tight' = 'Yellow'; 'too big' = 'DarkGray'; '' = 'Gray' }
    foreach ($f in $Catalog.families) {
        Say ''
        $color = if ($f.kind -eq 'uncensored') { 'Yellow' } else { 'White' }
        Say "$($f.label)$(if ($f.recommended) { ' (recommended)' })  -  $($f.maker)" $color
        Say "    $($f.about) $(Source-Line $f)."
        Say "    Good: $($f.good)"
        Say "    Weak: $($f.weak)"
        foreach ($sz in $f.sizes) {
            $mark = if ($have -contains $sz.id) { '[x]' } else { '[ ]' }
            $fit = Fit $sz $mib
            $line = "    $mark {0,-38} {1,-8} v{2,-4} {3,5} GB   card {4,2} GB   Mac {5,2} GB" -f $sz.id, $sz.size, $sz.version, $sz.size_gb, $sz.card_gb, $sz.mac_gb
            Say "$line$(if ($fit) { "   $fit" })$(if (-not $sz.tested) { '   (untested)' })" $colors[$fit]
        }
    }
    $own = Own $s
    if ($own.Count) {
        Say ''
        Say 'Your own models (not on the list, not checked by UVMC):' 'White'
        foreach ($k in $own.Keys) { Say "    [x] $k  -  $($own[$k])" }
    }
    Say ''
    Say 'Add one with local-model -Add <name> (the name in the first column), or your own file with -AddFile.'
    Say 'The panel (Settings > Models on your PC) picks which one UVMC uses. (untested): local-model -Test checks it after adding.'
}

function Post-Json($path, $body, $key, $timeout) {
    $json = $body | ConvertTo-Json -Depth 10 -Compress
    Invoke-RestMethod "http://127.0.0.1:$Port$path" -Method Post -ContentType 'application/json' `
        -Headers @{ Authorization = "Bearer $key" } -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec $timeout
}

function Test-Model($s, $name) {
    # The same request UVMC makes: a system prompt, a task, its run_command tool. Passes when the
    # model asks for the tool with a sensible command. Then a plain reply, to measure speed.
    $key = Api-Key $s
    Say ''
    Say "Testing $(Label $name)... (loading it on the card first can take a minute)"
    $tool = @{ type = 'function'; function = @{ name = 'run_command'
        description = 'Run a bash command as the agent user and return its exit code and combined output.'
        parameters = @{ type = 'object'; properties = @{ command = @{ type = 'string'; description = 'The bash command to run.' } }; required = @('command') } } }
    $body = @{ model = $name; max_tokens = 400; tool_choice = 'auto'; tools = @($tool)
        messages = @(@{ role = 'system'; content = 'You are an agent working on a Linux machine. Use the tools to do the task.' },
                     @{ role = 'user'; content = 'How many files are in the folder /tmp/demo? Find out with a command.' }) }
    $t = [Diagnostics.Stopwatch]::StartNew()
    try { $r = Post-Json '/v1/chat/completions' $body $key 600 }
    catch { Say "  Failed: $($_.Exception.Message). See $(Join-Path $s.dir 'server.log')." 'Red'; return $false }
    $first = [math]::Round($t.Elapsed.TotalSeconds)
    $call = @($r.choices[0].message.tool_calls)[0]
    $cmd = ''
    if ($call) { try { $cmd = ($call.function.arguments | ConvertFrom-Json).command } catch { $cmd = '' } }
    $toolOk = $call -and $call.function.name -eq 'run_command' -and $cmd -match '/tmp/demo'
    if ($toolOk) { Say "  Tool call: right (run_command: $cmd)" 'Green' }
    elseif ($call) { Say "  Tool call: wrong ($($call.function.name): $($call.function.arguments))" 'Red' }
    else { Say "  Tool call: none. It wrote instead: $(([string]$r.choices[0].message.content).Trim() -replace '\s+', ' ')" 'Red' }
    $body = @{ model = $name; max_tokens = 200
        messages = @(@{ role = 'user'; content = 'Write a short paragraph about why backups matter.' }) }
    try { $r = Post-Json '/v1/chat/completions' $body $key 600 } catch { Say "  Failed: $($_.Exception.Message)" 'Red'; return $false }
    $w = [math]::Round([double]$r.timings.predicted_per_second)
    Say "  Speed: $w tokens a second writing. First answer after $first s (loading included)."
    return [bool]$toolOk
}

function Show-Status($s) {
    if (-not $s.PSObject.Properties['dir']) { Say 'Not set up yet. Run local-model.'; return }
    Say "Folder:    $($s.dir)"
    Say "llama.cpp: $($s.llama)"
    Say "Card:      $($s.card) ($($s.card_mib) MiB)"
    Say "Models:    $(((Installed $s) | ForEach-Object { Label $_ }) -join ', ')"
    Say "Starts at login: $(if (Test-Path $Startup) { 'yes (local-model -NoAutoStart to stop that)' } else { 'no (local-model -AutoStart if you want it)' })"
    if (-not (Running)) { Say 'Not running (local-model -Start).' 'Yellow'; return }
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$Port/models" -Headers @{ Authorization = "Bearer $(Api-Key $s)" } -TimeoutSec 3
        $loaded = @($r.data | Where-Object { $_.status.value -eq 'loaded' } | ForEach-Object { $_.id })
        Say "Running, port $Port. On the card now: $(if ($loaded) { $loaded -join ', ' } else { 'none (loaded when UVMC asks)' })." 'Green'
    } catch { Say "Running, port $Port." 'Green' }
}

# --- main -----------------------------------------------------------------------------
$state = Load-State
if ($Dir) { Set-Field $state 'dir' ([IO.Path]::GetFullPath($Dir)) }
elseif (-not $state.PSObject.Properties['dir']) {
    Set-Field $state 'dir' (Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'uvmc-local')
}
Forget-OldModel $state
if ($Model) { $Add = $Model }   # -Model was "switch to"; now models are kept side by side

if ($Status) { Show-Status $state; exit 0 }
if ($List) { Show-List $state; exit 0 }
if ($AutoStart -or $NoAutoStart) {
    if ($AutoStart -and -not (Installed $state)) { Fail 'Not set up yet. Run local-model first.' }
    Set-AutoStart $AutoStart; exit 0
}
if ($Stop) { Stop-Server; exit 0 }
if ($Logs) {
    Set-Field $state 'logs' ($Logs -eq 'on'); Save-State $state
    Say $(if ($Logs -eq 'on') { "The server's normal log is on (requests and timings, not what's written), in $(Join-Path $state.dir 'server.log'). Turn it off with -Logs off." } else { 'The server logs only warnings and errors.' }) 'Green'
    if (Running) { Stop-Server; Start-Server $state }
    exit 0
}
if ($Start) {
    if (-not (Installed $state)) { Fail 'Not set up yet. Run local-model first.' }
    Start-Server $state; exit 0
}
if ($Test) {
    if (-not (Running)) { Fail 'The model server is not running (local-model -Start).' }
    $which = if ($Rest) { @($Rest) } else { @(Installed $state) }
    $bad = @($which | Where-Object { (Installed $state) -notcontains $_ })
    if ($bad) { Fail "Not downloaded: $($bad -join ', '). Downloaded: $((Installed $state) -join ', ')." }
    $failed = 0
    foreach ($n in $which) { if (-not (Test-Model $state $n)) { $failed++ } }
    Say ''
    if ($failed) { Say "$failed of $($which.Count) did not call the tool right: UVMC's agent would struggle with them." 'Yellow'; exit 1 }
    Say 'All passed.' 'Green'; exit 0
}
if ($Delete) {
    if ((Installed $state) -notcontains $Delete) { Fail "'$Delete' is not downloaded. Downloaded: $((Installed $state) -join ', ')." }
    $rest = @(Installed $state | Where-Object { $_ -ne $Delete })
    if (-not $rest) { Fail 'It is the only model left. local-model -Remove removes everything.' }
    $path = Model-Path $state $Delete
    $label = Label $Delete
    $own = Own $state
    $isOwn = $own.Contains($Delete)
    if ($isOwn) { $own.Remove($Delete); Set-Field $state 'own' ([pscustomobject]$own) }
    else { Set-Field $state 'models' @($state.models | Where-Object { $_ -ne $Delete }) }
    Save-State $state
    Write-Preset $state
    Restart-IfRunning $state
    if ($isOwn) { Say "Forgot $label. Your file stays where it is: $path" 'Green'; exit 0 }
    if (Test-Path $path) { Remove-Item $path }
    Say "Deleted $label. If the panel had picked it, UVMC now uses another one until you pick again." 'Green'
    exit 0
}
if ($Remove) {
    if (-not $Yes) {
        $a = Read-Host "Delete $($state.dir) (llama.cpp and the models) and the firewall rules? [y/N]"
        if ($a -notmatch '^[yY]') { exit 0 }
    }
    Stop-Server
    Remove-Item $Startup -ErrorAction SilentlyContinue
    $rules = @($RuleName, $OutRuleName) | Where-Object {
        (& netsh advfirewall firewall show rule name="$_" 2>$null | Out-String) -match [regex]::Escape($_)
    }
    if ($rules) {
        $cmd = ($rules | ForEach-Object { "Remove-NetFirewallRule -DisplayName '$_'" }) -join '; '
        Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden `
            -ArgumentList @('-NoProfile', '-Command', $cmd)
    }
    if (Test-Path $state.dir) { Remove-Item -Recurse -Force $state.dir }
    $mp = Find-Multipass
    if ($mp) { Send-Key $mp '' | Out-Null }   # an empty key removes it
    Remove-Item $StateFile -ErrorAction SilentlyContinue
    Say 'Removed.' 'Green'; exit 0
}
if ($Add -and -not $Models.Contains($Add)) { Fail "Unknown model '$Add'. local-model -List shows them: $($Models.Keys -join ', ')." }
if ($AddFile) {
    # A GGUF file of your own: used where it is, with your card's settings. Its contents are not checked
    # (no pinned checksum), and it's marked as your own in the panel.
    if (-not (Test-Path (Server-Exe $state))) { Fail 'Not set up yet. Run local-model first, then add your own file.' }
    $file = [IO.Path]::GetFullPath($AddFile)
    if (-not (Test-Path $file -PathType Leaf) -or $file -notmatch '\.gguf$') { Fail "$AddFile is not a .gguf file." }
    $id = if ($Name) { $Name } else { [IO.Path]::GetFileNameWithoutExtension($file) }
    $id = ($id.ToLower() -replace '[^a-z0-9._-]+', '-').Trim('-', '.')
    if ($id.Length -gt 60) { $id = $id.Substring(0, 60).TrimEnd('-', '.') }
    if (-not $id -or $id -eq 'local') { Fail 'Give it a name with -Name (letters, digits, dots and dashes).' }
    if ($Models.Contains($id)) { Fail "'$id' is a model on UVMC's list: local-model -Add $id gets the checked file. Use -Name for yours." }
    $own = Own $state
    $own[$id] = $file
    Set-Field $state 'own' ([pscustomobject]$own)
    Save-State $state
    Write-Preset $state
    Restart-IfRunning $state
    if (-not (Running)) { Start-Server $state }
    Say "Added your own model as '$id' ($file). UVMC hasn't checked this file: only use files from sources you trust." 'Yellow'
    Say "Pick it in the panel (Settings > Models on your PC), and try it with local-model -Test $id." 'Green'
    exit 0
}

# Set up, add a model, or update.
New-Item -ItemType Directory -Force $state.dir | Out-Null
Say "Folder: $($state.dir)"
if ($Update -or -not (Test-Path (Server-Exe $state))) { Get-LlamaCpp $state }

$card = Get-Card $state
if (-not $card) { Fail 'llama.cpp found no graphics card it can use. Is the graphics driver up to date?' }
Set-Field $state 'card' $card.Name; Set-Field $state 'card_mib' $card.MiB
Say "Graphics card: $($card.Name), $($card.MiB) MiB."

$name = if ($Add) { $Add } elseif (-not (Installed $state)) { Pick-Model $card.MiB } else { $null }
if ($name) {
    $m = $Models[$name]
    $fit = Fit $m $card.MiB
    if ($fit -eq 'tight') { Say "$($m.label) needs a $($m.card_gb) GB card: it fits on yours, but tightly, and may be slow." 'Yellow' }
    elseif ($fit -eq 'too big') { Say "$($m.label) needs a $($m.card_gb) GB card: it will spill into the PC's memory and be very slow on yours." 'Yellow' }
    Say "Model: $($m.label). $($m.about)"
    Get-Model $state $name
}

$keyFile = Join-Path $state.dir 'api_key'
if (-not (Test-Path $keyFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($keyFile, (-join ($bytes | ForEach-Object { $_.ToString('x2') })))
}
Save-State $state
Write-Preset $state

# Files that are no longer on the list (older lists, other versions) stay until you delete them.
$known = @($Models.Values | ForEach-Object { $_.file }) + @((Own $state).Values | ForEach-Object { Split-Path $_ -Leaf })
$mdir = Join-Path $state.dir 'models'
if (Test-Path $mdir) {
    Get-ChildItem $mdir -Filter *.gguf | Where-Object { $known -notcontains $_.Name } | ForEach-Object {
        Say "$($_.Name) is no longer on the list, so UVMC doesn't use it. Delete it by hand to free $([math]::Round($_.Length / 1GB, 1)) GB: $($_.FullName)" 'Yellow'
    }
}

if ($Add -and -not $Update) {
    # Only a model added: no need to look for the UVMC machine or touch the firewall again.
    Restart-IfRunning $state
    if (-not (Running)) { Start-Server $state }
    if ($state.PSObject.Properties['vm_interface']) {
        Say "Added. Pick it in the panel (Settings > Models), and try it with local-model -Test $Add." 'Green'
    } else {
        # A first set-up done with -Add: running for this PC only, not yet for UVMC.
        Say "Added, and running for this PC only (try it with local-model -Test $Add)." 'Green'
        Say 'UVMC is not connected yet: run local-model with no options to connect it (Windows asks for administrator rights once, for the firewall).' 'Yellow'
    }
    exit 0
}

$net = Find-VmNetwork
if ($net) { Set-Field $state 'vm_interface' $net.Interface; Save-State $state; Open-Firewall $state $net } else { Say 'The UVMC machine was not found; it can be connected later by running local-model again.' 'Yellow' }

if (Running) { Stop-Server }   # restart, so it listens on today's addresses
Start-Server $state

if ($net) { Connect-Uvmc $state }
Say 'Done. It runs until you stop it (local-model -Stop) or restart Windows; local-model -Start starts it again.'
Say 'More models: local-model -List.'
if (Test-Path $Startup) { Say 'It also starts each time you log in (local-model -NoAutoStart turns that off).' }
else { Say 'It does not start by itself when you log in; local-model -AutoStart does that, if you want it.' }
