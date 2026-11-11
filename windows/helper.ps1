# UVMC helper: a tray icon and a small local service that lets the panel do what only this PC
# can do (the model on your graphics card for now). Start it with helper.cmd; it runs hidden.
#
# How it is walled off:
#   - It listens on 127.0.0.1 only (a plain socket, not Windows' HTTP service, which listens on every
#     address), so the UVMC machine and your home network can't reach it.
#   - Every /pc/ call needs its key (kept in %LOCALAPPDATA%\UVMC\helper.json) and, from a browser,
#     must come from the panel's own address (one of the machine's current addresses, port 8080).
#   - The Host header must name 127.0.0.1 or localhost, so other websites can't reach it by DNS tricks.
#   - It does a fixed set of things, with names checked against UVMC's list of models. Deleting a model
#     and adding a file of your own are confirmed here on the PC, in its own window.
# The key reaches the panel only in the part of the address after "#", which the browser never sends
# to the machine. It logs what was asked and the answer's code, never keys or what conversations say.
#
#   /panel     opens the panel, signed in, at the machine's current address (from the tray or a shortcut)
#   /pc/...    the panel's calls (status, model actions, job progress)
#
#   helper.cmd                 start it (hidden; a second start opens the panel)
#   helper.cmd -AutoStart      start it each time you log in (a shortcut in your Startup folder), and now
#   helper.cmd -NoAutoStart    stop starting it at login
#   helper.cmd -Stop           quit the running helper
param([int]$Port = 8092, [switch]$AutoStart, [switch]$NoAutoStart, [switch]$Stop)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
. (Join-Path $PSScriptRoot 'timeouts.ps1')

$VmName = 'uvmc'
$PanelPort = 8080
$Root = Split-Path $PSScriptRoot
$LocalModel = Join-Path $PSScriptRoot 'local-model.ps1'
$CatalogFile = Join-Path $Root 'supervisor\uvmc\local_models.json'
$StateFile = Join-Path $env:LOCALAPPDATA 'UVMC\local-model.json'
$DataDir = Join-Path $env:LOCALAPPDATA 'UVMC'
$ConfigFile = Join-Path $DataDir 'helper.json'
$LogFile = Join-Path $DataDir 'helper.log'
$JobDir = Join-Path $DataDir 'helper-jobs'
$Version = 'unknown'
try {
    $v = Select-String -Path (Join-Path $Root 'supervisor\uvmc\__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"'
    if ($v) { $Version = $v.Matches[0].Groups[1].Value }
} catch {}

New-Item -ItemType Directory -Force $DataDir, $JobDir | Out-Null

function Log([string]$text) {
    try {
        $keep = @()
        if (Test-Path $LogFile) { $keep = @(Get-Content -Path $LogFile -Tail 999 -Encoding UTF8) }
        Set-Content -Path $LogFile -Value ($keep + ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $text)) -Encoding UTF8
    } catch {}
}

# --- start at login, stop -----------------------------------------------------------------
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'UVMC helper.lnk'
function Stop-Helper {
    $n = 0
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*$PSCommandPath*" -and $_.CommandLine -notmatch '-(Stop|AutoStart|NoAutoStart)\b' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }
    return $n
}
if ($Stop) {
    if ((Stop-Helper) -gt 0) { Write-Host 'The UVMC helper is stopped.' } else { Write-Host 'The UVMC helper was not running.' }
    exit 0
}
# Start at login: only for you (a shortcut in your Startup folder, no administrator rights), and
# only when you ask (helper -AutoStart, the installer's question, or the tray menu's checkmark).
# Started through the shortcut, by Explorer, so it never runs with administrator rights even when
# an installer asks for it.
function Set-HelperAutoStart([bool]$on) {
    if (-not $on) { Remove-Item $StartupLink -ErrorAction SilentlyContinue; return }
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($StartupLink)
    $lnk.TargetPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File `"$PSCommandPath`""
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.WindowStyle = 7
    $lnk.Description = 'UVMC helper: lets the UVMC panel manage the model on this PC'
    $lnk.Save()
}
if ($NoAutoStart) {
    if (Test-Path $StartupLink) { Set-HelperAutoStart $false; Write-Host 'The UVMC helper no longer starts when you log in.' }
    else { Write-Host 'The UVMC helper was not starting at login.' }
    exit 0
}
if ($AutoStart) {
    Set-HelperAutoStart $true
    Write-Host 'The UVMC helper now starts each time you log in (helper -NoAutoStart, or the tray menu, turns that off).'
    $up = $false
    try { $c = New-Object Net.Sockets.TcpClient; $up = $c.ConnectAsync('127.0.0.1', $Port).Wait(500); $c.Close() } catch {}
    if (-not $up) { Start-Process explorer.exe -ArgumentList "`"$StartupLink`"" }
    exit 0
}

# --- one copy only ----------------------------------------------------------------------
# A second start (double-clicking helper.cmd again) just opens the panel through the running one.
$created = $false
$script:Mutex = New-Object Threading.Mutex($true, 'Local\UVMC-helper', [ref]$created)
if (-not $created) { Start-Process "http://127.0.0.1:$Port/panel"; exit 0 }

# --- its key ----------------------------------------------------------------------------
function New-Key {
    $b = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString('x2') })
}
$config = $null
if (Test-Path $ConfigFile) { try { $config = Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch { $config = $null } }
if (-not $config -or -not $config.key -or $config.key.Length -lt 32) {
    $config = [pscustomobject]@{ key = (New-Key); port = $Port }
    [IO.File]::WriteAllText($ConfigFile, ($config | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
$Key = $config.key

# --- small helpers ----------------------------------------------------------------------
function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}

# Runs a program hidden, with a limit; returns @{ Out; Code } (Code -1 when it was stopped).
function Invoke-Hidden([string]$File, [string[]]$ArgList, [int]$Seconds) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = ($ArgList | ForEach-Object { Format-NativeArg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true   # closed at once: "multipass exec" hangs on the hidden console's input
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEndAsync()
    $null = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($Seconds * 1000)) { try { $p.Kill() } catch {}; return @{ Out = ''; Code = -1 } }
    $p.WaitForExit()
    return @{ Out = $out.Result; Code = $p.ExitCode }
}

# The machine's state and addresses, from "multipass list" (no exec, so none of exec's CPU trouble).
# Kept 30 s; Windows can give the machine a new address while it runs, so it's looked up again often.
$script:VmCache = $null
function Get-Vm([switch]$Fresh) {
    if (-not $Fresh -and $script:VmCache -and ((Get-Date) - $script:VmCache.At).TotalSeconds -lt 30) { return $script:VmCache }
    $info = @{ State = 'unknown'; Ips = @(); At = (Get-Date) }
    $mp = Find-Multipass
    if (-not $mp) { $info.State = 'no multipass' }
    else {
        $r = Invoke-Hidden $mp @('list', '--format', 'json') 20
        if ($r.Code -eq 0) {
            try {
                $vm = (ConvertFrom-Json $r.Out).list | Where-Object { $_.name -eq $VmName } | Select-Object -First 1
                if ($vm) { $info.State = $vm.state; $info.Ips = @($vm.ipv4 | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' }) }
                else { $info.State = 'not installed' }
            } catch {}
        } elseif ($r.Code -eq -1) { Write-TimeoutLog 'helper' 'multipass list' 'no answer from Multipass, stopped after 20 s' }
    }
    $script:VmCache = $info
    return $info
}

function Test-PanelOrigin([string]$Origin) {
    if ($Origin -notmatch "^http://(\d{1,3}(\.\d{1,3}){3}):$PanelPort$") { return $false }
    $ip = $Matches[1]
    if ((Get-Vm).Ips -contains $ip) { return $true }
    return ((Get-Vm -Fresh).Ips -contains $ip)
}

function Get-Catalog {
    $c = [IO.File]::ReadAllText($CatalogFile) | ConvertFrom-Json
    $m = [ordered]@{}
    foreach ($f in $c.families) { foreach ($s in $f.sizes) { $m[$s.id] = $s } }
    return $m
}
function Get-ModelState {
    if (Test-Path $StateFile) { try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
    return $null
}
function Get-Installed($s) {
    if (-not $s) { return @() }
    $list = @()
    if ($s.PSObject.Properties['models']) { $list += @($s.models) }
    if ($s.PSObject.Properties['own'] -and $s.own) { $list += @($s.own.PSObject.Properties | ForEach-Object { $_.Name }) }
    return @($list | Where-Object { $_ })
}
function Test-ServerRunning($s) {
    if (-not $s -or -not $s.PSObject.Properties['dir']) { return $false }
    return [bool](Get-Process llama-server -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$($s.dir)*" })
}

# A window of its own on top of everything, so a question isn't hidden behind the browser.
function Ask-Owner {
    $f = New-Object Windows.Forms.Form
    $f.TopMost = $true; $f.ShowInTaskbar = $false; $f.Opacity = 0
    $f.StartPosition = 'CenterScreen'; $f.Size = New-Object Drawing.Size(1, 1)
    $f.Show(); $f.Activate()
    return $f
}
function Confirm-OnPc([string]$Text) {
    $o = Ask-Owner
    try { return ([Windows.Forms.MessageBox]::Show($o, $Text, 'UVMC', 'YesNo', 'Warning', 'Button2') -eq 'Yes') }
    finally { $o.Close(); $o.Dispose() }
}
function Pick-Gguf {
    $o = Ask-Owner
    try {
        $d = New-Object Windows.Forms.OpenFileDialog
        $d.Title = 'UVMC: choose a GGUF model file of your own'
        $d.Filter = 'GGUF models (*.gguf)|*.gguf'
        if ($d.ShowDialog($o) -eq 'OK') { return $d.FileName }
        return $null
    } finally { $o.Close(); $o.Dispose() }
}

# --- jobs: one long action at a time, run hidden, its output kept for the panel ---------------
$script:Jobs = [ordered]@{}
$script:JobCount = 0
Get-ChildItem $JobDir -File -ErrorAction SilentlyContinue | Remove-Item -ErrorAction SilentlyContinue

function Get-BusyJob {
    foreach ($j in $script:Jobs.Values) { if (-not $j.Process.HasExited) { return $j } }
    return $null
}
function Quote-Ps([string]$s) { "'" + ($s -replace "'", "''") + "'" }

# $What is shown in notices ("Downloading qwen3.5-9b"). $Command is PowerShell run in its own hidden process.
function Start-HelperJob([string]$Action, [string]$What, [string]$Command) {
    $script:JobCount++
    $id = '{0}-{1}' -f (Get-Date -Format 'yyyyMMddHHmmss'), $script:JobCount
    $out = Join-Path $JobDir "$id.txt"
    $full = "$Command *> $(Quote-Ps $out); exit `$LASTEXITCODE"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($full))
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = 'powershell.exe'
    $psi.Arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true   # closed at once, so the job's own "multipass exec" calls can't hang
    $psi.EnvironmentVariables['UVMC_HELPER'] = '1'   # local-model prints "Progress: …" lines for downloads
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $script:Jobs[$id] = @{ Id = $id; Action = $Action; What = $What; Process = $p; Out = $out; Started = (Get-Date); Told = $false }
    while ($script:Jobs.Count -gt 20) {
        $old = @($script:Jobs.Keys)[0]
        if (-not $script:Jobs[$old].Process.HasExited) { break }
        Remove-Item $script:Jobs[$old].Out -ErrorAction SilentlyContinue
        $script:Jobs.Remove($old)
    }
    Log "job $id started: $Action"
    return $id
}
function Read-JobLines($j, [int]$Count = 60) {
    if (-not (Test-Path $j.Out)) { return @() }
    try {
        $fs = [IO.File]::Open($j.Out, 'Open', 'Read', 'ReadWrite')
        try { $text = (New-Object IO.StreamReader($fs, $true)).ReadToEnd() } finally { $fs.Dispose() }
        $lines = @($text -split "\r?\n" | Where-Object { $_.Trim() -ne '' })
        return @($lines | Select-Object -Last $Count)
    } catch { return @() }
}
# "Progress: 37% (2.1 of 5.6 GB)" lines become one progress field (the newest); the rest stay as lines.
function Job-Json($j) {
    $done = $j.Process.HasExited
    $code = if ($done) { $j.Process.ExitCode } else { $null }
    $all = @(Read-JobLines $j 400)
    $progress = $null
    $p = @($all | Where-Object { $_ -match '^Progress: \d+%' }) | Select-Object -Last 1
    if ($p -and $p -match '^Progress: (\d+)%\s*(?:\((.*)\))?') { $progress = [ordered]@{ percent = [int]$Matches[1]; text = [string]$Matches[2] } }
    return [ordered]@{
        id = $j.Id; action = $j.Action; what = $j.What
        state = if (-not $done) { 'running' } elseif ($code -eq 0) { 'done' } else { 'failed' }
        exit = $code; started = $j.Started.ToString('s'); progress = $progress
        lines = @($all | Where-Object { $_ -notmatch '^Progress: \d+%' } | Select-Object -Last 60)
    }
}

function Start-ModelJob([string]$Action, [string]$What, [string[]]$ArgList) {
    # Option names go in bare (a quoted '-Test' would be a plain word, and local-model would run its
    # full set-up instead); everything else is quoted.
    $parts = foreach ($a in $ArgList) { if ($a -cmatch '^-[A-Z][A-Za-z]+$') { $a } else { Quote-Ps $a } }
    $cmd = '& ' + (Quote-Ps $LocalModel) + ' ' + ($parts -join ' ')
    return Start-HelperJob $Action $What $cmd
}

# --- what the panel may ask ----------------------------------------------------------------
$NamePattern = '^[a-z0-9][a-z0-9._-]{0,59}$'

function Do-ModelAction($body) {
    $busy = Get-BusyJob
    if ($busy) { return (Reply-Json 409 @{ error = "Busy: $($busy.What). Try again when it's done."; job = $busy.Id }) }
    $action = [string]$body.action
    $name = [string]$body.name
    $value = [string]$body.value
    $s = Get-ModelState
    $installed = @(Get-Installed $s)
    $set = ($s -and (Test-Path (Join-Path ([string]$s.dir) 'llama.cpp\llama-server.exe')))
    switch ($action) {
        'add' {
            $cat = Get-Catalog
            if (-not $cat.Contains($name)) { return (Reply-Json 400 @{ error = "Not on UVMC's list of models: $name" }) }
            if (-not $set) { return (Reply-Json 409 @{ error = 'The model on your PC is not set up yet. Run local-model once on the PC first.' }) }
            $id = Start-ModelJob 'add' "Downloading $($cat[$name].label)" @('-Add', $name)
        }
        'delete' {
            if ($installed -notcontains $name) { return (Reply-Json 400 @{ error = "Not downloaded: $name" }) }
            $cat = Get-Catalog
            $label = if ($cat.Contains($name)) { $cat[$name].label } else { "$name (your own file, which stays where it is)" }
            if (-not (Confirm-OnPc "Delete the model $label from this PC?")) { return (Reply-Json 200 @{ cancelled = $true }) }
            $id = Start-ModelJob 'delete' "Deleting $label" @('-Delete', $name)
        }
        'addfile' {
            if (-not $set) { return (Reply-Json 409 @{ error = 'The model on your PC is not set up yet. Run local-model once on the PC first.' }) }
            if ($name -and $name -notmatch $NamePattern) { return (Reply-Json 400 @{ error = 'A name has letters, digits, dots and dashes (up to 60).' }) }
            $file = Pick-Gguf
            if (-not $file) { return (Reply-Json 200 @{ cancelled = $true }) }
            if (-not (Confirm-OnPc "Use this file as a model?`n`n$file`n`nUVMC can't check a file of your own: only use files from sources you trust.")) { return (Reply-Json 200 @{ cancelled = $true }) }
            $argList = @('-AddFile', $file)
            if ($name) { $argList += @('-Name', $name) }
            $id = Start-ModelJob 'addfile' "Adding $([IO.Path]::GetFileName($file))" $argList
        }
        'start' { if (-not $installed) { return (Reply-Json 409 @{ error = 'No model downloaded yet.' }) }; $id = Start-ModelJob 'start' 'Starting the model server' @('-Start') }
        'stop' { $id = Start-ModelJob 'stop' 'Stopping the model server' @('-Stop') }
        'test' {
            if ($name -and $installed -notcontains $name) { return (Reply-Json 400 @{ error = "Not downloaded: $name" }) }
            $argList = @('-Test'); if ($name) { $argList += $name }
            $id = Start-ModelJob 'test' "Testing $(if ($name) { $name } else { 'the models' })" $argList
        }
        'update' {
            if (-not $set) { return (Reply-Json 409 @{ error = 'The model on your PC is not set up yet.' }) }
            $id = Start-ModelJob 'update' 'Updating llama.cpp' @('-Update')
        }
        'logs' {
            if ($value -notin @('on', 'off')) { return (Reply-Json 400 @{ error = 'logs is on or off' }) }
            $id = Start-ModelJob 'logs' "Turning the server's log $value" @('-Logs', $value)
        }
        'autostart' {
            if ($value -notin @('on', 'off')) { return (Reply-Json 400 @{ error = 'autostart is on or off' }) }
            $id = Start-ModelJob 'autostart' "Start at login: $value" @($(if ($value -eq 'on') { '-AutoStart' } else { '-NoAutoStart' }))
        }
        default { return (Reply-Json 400 @{ error = "Unknown action: $action" }) }
    }
    return (Reply-Json 200 @{ job = $id })
}

function Get-Status {
    $vm = Get-Vm
    $s = Get-ModelState
    $busy = Get-BusyJob
    $model = [ordered]@{ setup = $false }
    if ($s -and $s.PSObject.Properties['dir']) {
        $model = [ordered]@{
            setup = (Test-Path (Join-Path ([string]$s.dir) 'llama.cpp\llama-server.exe'))
            running = (Test-ServerRunning $s)
            installed = @(Get-Installed $s)
            llama = [string]$s.llama
            card = [string]$s.card
            card_mib = $s.card_mib
            logs = [bool]($s.PSObject.Properties['logs'] -and $s.logs)
            autostart = (Test-Path (Join-Path ([Environment]::GetFolderPath('Startup')) 'UVMC local model.lnk'))
        }
    }
    return [ordered]@{
        helper = $Version; vm = [ordered]@{ state = $vm.State; ipv4 = @($vm.Ips) }
        model = $model; job = $(if ($busy) { $busy.Id } else { $null })
    }
}

# --- /panel: open the panel at the machine's current address -------------------------------
function Page([string]$Title, [string]$Text, [int]$Refresh = 0) {
    $meta = if ($Refresh) { "<meta http-equiv=`"refresh`" content=`"$Refresh`">" } else { '' }
    $t = [Net.WebUtility]::HtmlEncode($Title); $x = [Net.WebUtility]::HtmlEncode($Text)
    return "<!doctype html><html><head><meta charset=`"utf-8`">$meta<title>$t</title><style>body{font:16px system-ui,sans-serif;max-width:36em;margin:4em auto;padding:0 1em;color:#222;background:#fafafa}@media(prefers-color-scheme:dark){body{color:#ddd;background:#1b1b1b}}</style></head><body><h2>$t</h2><p>$x</p></body></html>"
}
function Test-PanelUp([string]$Ip) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $ok = $c.ConnectAsync($Ip, $PanelPort).Wait(1500)
        $c.Close(); return $ok
    } catch { return $false }
}
function Open-Panel($req) {
    # Only from a shortcut, the tray or the address bar, never from another website's link or script.
    $site = $req.Headers['sec-fetch-site']
    if ($site -and $site -ne 'none') { return (Reply-Html 403 (Page 'Not from here' 'Open the UVMC panel from its shortcut or the tray icon.')) }
    $vm = Get-Vm -Fresh
    if ($vm.State -ne 'Running') {
        return (Reply-Html 200 (Page 'UVMC is not running' "The UVMC machine is $($vm.State.ToLower()). Start it with the UVMC Panel shortcut (it also shares your folders), then open the panel again."))
    }
    $ip = @($vm.Ips)[0]
    if (-not $ip -or -not (Test-PanelUp $ip)) { return (Reply-Html 200 (Page 'UVMC is starting' 'The panel is not answering yet. This page tries again by itself.' 3)) }
    # Fully hidden, with its own pipes: the helper's hidden console makes "multipass exec" hang.
    $mp = Find-Multipass
    $r = Invoke-Hidden $mp @('exec', $VmName, '--', 'timeout', '-k', '5', '15', 'sudo', 'cat', '/etc/uvmc/panel_token') 25
    $token = ([string]$r.Out).Trim()
    if ($r.Code -ne 0 -or -not $token) {
        $why = if ($r.Code -eq -1) { 'Multipass did not answer within 25 seconds.' } elseif (Test-VmTimedOut $r.Code) { 'The machine did not answer within 15 seconds.' } else { '' }
        if ($r.Code -eq -1 -or (Test-VmTimedOut $r.Code)) { Write-TimeoutLog 'helper' 'sudo cat /etc/uvmc/panel_token' $why }
        return (Reply-Html 200 (Page 'UVMC did not answer' "The panel's key could not be read from the machine. $why This page tries again by itself." 5))
    }
    $loc = 'http://{0}:{1}/?token={2}#pc={3}' -f $ip, $PanelPort, [Uri]::EscapeDataString($token), $Key
    return @{ Code = 302; Body = ''; Type = 'text/plain'; Headers = @{ Location = $loc; 'Referrer-Policy' = 'no-referrer' } }
}

# --- HTTP on a plain socket ------------------------------------------------------------------
function Reply-Json([int]$Code, $Obj) { @{ Code = $Code; Body = ($Obj | ConvertTo-Json -Compress -Depth 6); Type = 'application/json; charset=utf-8'; Headers = @{} } }
function Reply-Html([int]$Code, [string]$Html) { @{ Code = $Code; Body = $Html; Type = 'text/html; charset=utf-8'; Headers = @{} } }

function Read-Request($stream) {
    $buf = New-Object byte[] 8192
    $ms = New-Object IO.MemoryStream
    $end = -1
    while ($end -lt 0) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { return $null }
        $ms.Write($buf, 0, $n)
        if ($ms.Length -gt 16384) { return $null }
        $text = [Text.Encoding]::ASCII.GetString($ms.ToArray())
        $end = $text.IndexOf("`r`n`r`n")
    }
    $lines = $text.Substring(0, $end) -split "`r`n"
    $first = $lines[0] -split ' '
    if ($first.Count -ne 3) { return $null }
    $h = @{}
    foreach ($l in ($lines | Select-Object -Skip 1)) {
        $i = $l.IndexOf(':')
        if ($i -gt 0) { $h[$l.Substring(0, $i).Trim().ToLower()] = $l.Substring($i + 1).Trim() }
    }
    $len = 0
    if ($h['content-length']) { if (-not [int]::TryParse($h['content-length'], [ref]$len) -or $len -lt 0 -or $len -gt 65536) { return $null } }
    $all = $ms.ToArray()
    $start = $end + 4
    $body = New-Object byte[] $len
    $have = [Math]::Min($len, $all.Length - $start)
    if ($have -gt 0) { [Array]::Copy($all, $start, $body, 0, $have) }
    while ($have -lt $len) {
        $n = $stream.Read($body, $have, $len - $have)
        if ($n -le 0) { return $null }
        $have += $n
    }
    return @{ Method = $first[0]; Path = ($first[1] -split '\?')[0]; Headers = $h; Body = [Text.Encoding]::UTF8.GetString($body) }
}

function Send-Reply($stream, $res, [string]$Origin) {
    $reasons = @{ 200 = 'OK'; 204 = 'No Content'; 302 = 'Found'; 400 = 'Bad Request'; 401 = 'Unauthorized'; 403 = 'Forbidden'
                  404 = 'Not Found'; 405 = 'Method Not Allowed'; 409 = 'Conflict'; 500 = 'Internal Server Error' }
    $bytes = [Text.Encoding]::UTF8.GetBytes([string]$res.Body)
    $sb = New-Object Text.StringBuilder
    [void]$sb.Append("HTTP/1.1 $($res.Code) $($reasons[[int]$res.Code])`r`n")
    [void]$sb.Append("Content-Length: $($bytes.Length)`r`nConnection: close`r`nCache-Control: no-store`r`nX-Content-Type-Options: nosniff`r`n")
    if ($res.Type) { [void]$sb.Append("Content-Type: $($res.Type)`r`n") }
    if ($Origin) { [void]$sb.Append("Access-Control-Allow-Origin: $Origin`r`nVary: Origin`r`n") }
    foreach ($k in $res.Headers.Keys) { [void]$sb.Append("${k}: $($res.Headers[$k])`r`n") }
    [void]$sb.Append("`r`n")
    $head = [Text.Encoding]::ASCII.GetBytes($sb.ToString())
    $stream.Write($head, 0, $head.Length)
    if ($bytes.Length) { $stream.Write($bytes, 0, $bytes.Length) }
    $stream.Flush()
}

function Route($req) {
    $h = $req.Headers
    if ($h['host'] -notin @("127.0.0.1:$Port", "localhost:$Port")) { return @{ Res = (Reply-Json 403 @{ error = 'wrong host' }) } }
    if ($req.Path -eq '/panel') {
        if ($req.Method -ne 'GET') { return @{ Res = (Reply-Json 405 @{ error = 'GET only' }) } }
        return @{ Res = (Open-Panel $req) }
    }
    if ($req.Path -eq '/') { return @{ Res = (Reply-Html 200 (Page 'UVMC helper' "Running (UVMC $Version). Open the panel from the tray icon.")) } }
    if (-not $req.Path.StartsWith('/pc/')) { return @{ Res = (Reply-Json 404 @{ error = 'not found' }) } }

    # From a browser: only the panel's own page. Without an Origin (a script on this PC): the key alone.
    $origin = $h['origin']
    if ($origin -and -not (Test-PanelOrigin $origin)) { return @{ Res = (Reply-Json 403 @{ error = 'not from the panel' }) } }
    if ($req.Method -eq 'OPTIONS') {
        return @{ Origin = $origin; Res = @{ Code = 204; Body = ''; Type = $null; Headers = @{
            'Access-Control-Allow-Methods' = 'GET, POST'; 'Access-Control-Allow-Headers' = 'Authorization, Content-Type'
            'Access-Control-Allow-Private-Network' = 'true'; 'Access-Control-Max-Age' = '600' } } }
    }
    if ($h['authorization'] -ne "Bearer $Key") { return @{ Origin = $origin; Res = (Reply-Json 401 @{ error = 'key missing or wrong' }) } }

    $res = $null
    if ($req.Path -eq '/pc/status' -and $req.Method -eq 'GET') { $res = Reply-Json 200 (Get-Status) }
    elseif ($req.Path -eq '/pc/model' -and $req.Method -eq 'POST') {
        $body = $null
        try { $body = ConvertFrom-Json $req.Body } catch {}
        if (-not $body -or $body -isnot [pscustomobject]) { $res = Reply-Json 400 @{ error = 'send a JSON object' } }
        else { $res = Do-ModelAction $body }
    }
    elseif ($req.Path -match '^/pc/jobs/([0-9]{14}-[0-9]+)$' -and $req.Method -eq 'GET') {
        $j = $script:Jobs[$Matches[1]]
        $res = if ($j) { Reply-Json 200 (Job-Json $j) } else { Reply-Json 404 @{ error = 'no such job' } }
    }
    else { $res = Reply-Json 404 @{ error = 'not found' } }
    return @{ Origin = $origin; Res = $res }
}

function Serve($client) {
    $what = '?'
    try {
        $client.ReceiveTimeout = 3000; $client.SendTimeout = 5000
        $stream = $client.GetStream()
        $req = Read-Request $stream
        if (-not $req) { return }
        $what = "$($req.Method) $($req.Path)"
        $out = Route $req
        Send-Reply $stream $out.Res $out.Origin
        Log "$what $($out.Res.Code)"
    } catch {
        Log "$what error: $($_.Exception.Message)"
    } finally { $client.Close() }
}

# --- tray ----------------------------------------------------------------------------------
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
try { $listener.Start() }
catch {
    Log "could not listen on 127.0.0.1:${Port}: $($_.Exception.Message)"
    [Windows.Forms.MessageBox]::Show("The UVMC helper could not start: port $Port on this PC is in use by another program.", 'UVMC') | Out-Null
    exit 1
}
Log "started (UVMC $Version), listening on 127.0.0.1:$Port"

$icon = New-Object Windows.Forms.NotifyIcon
$icon.Icon = [Drawing.Icon]::ExtractAssociatedIcon((Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'))
$icon.Text = 'UVMC'
function Notify([string]$Text, [string]$Kind = 'Info') { $icon.ShowBalloonTip(5000, 'UVMC', $Text, $Kind) }

function Open-PanelFromTray {
    $vm = Get-Vm -Fresh
    if ($vm.State -eq 'Running') { Start-Process "http://127.0.0.1:$Port/panel" }
    else { Start-Process (Join-Path $PSScriptRoot 'panel.cmd') }   # starts the machine and shares the folders first
}

$menu = New-Object Windows.Forms.ContextMenuStrip
$null = $menu.Items.Add('Open the UVMC panel', $null, { try { Open-PanelFromTray } catch { Log "open: $($_.Exception.Message)" } })
$null = $menu.Items.Add('-')
$modelMenu = New-Object Windows.Forms.ToolStripMenuItem 'Model on this PC'
$null = $modelMenu.DropDownItems.Add('Start', $null, {
    if (Get-BusyJob) { Notify 'Busy with something else; try again in a moment.'; return }
    $null = Start-ModelJob 'start' 'Starting the model server' @('-Start') })
$null = $modelMenu.DropDownItems.Add('Stop', $null, {
    if (Get-BusyJob) { Notify 'Busy with something else; try again in a moment.'; return }
    $null = Start-ModelJob 'stop' 'Stopping the model server' @('-Stop') })
$null = $menu.Items.Add($modelMenu)
$null = $menu.Items.Add('Stop UVMC', $null, {
    if (Get-BusyJob) { Notify 'Busy with something else; try again in a moment.'; return }
    if (-not (Confirm-OnPc 'Stop the UVMC machine? Whatever its agents are doing is interrupted. Everything inside is kept.')) { return }
    $null = Start-HelperJob 'vm-stop' 'Stopping UVMC' ('& cmd.exe /c ' + (Quote-Ps (Join-Path $PSScriptRoot 'stop.cmd'))) })
$null = $menu.Items.Add('-')
# Start at login, on or off; the checkmark shows how it is now (read again each time the menu opens).
$loginItem = New-Object Windows.Forms.ToolStripMenuItem 'Start the helper when I log in'
$loginItem.Add_Click({
    try {
        $on = -not (Test-Path $StartupLink)
        Set-HelperAutoStart $on
        $loginItem.Checked = (Test-Path $StartupLink)
        Notify $(if ($loginItem.Checked) { 'The helper now starts each time you log in.' } else { 'The helper no longer starts when you log in.' })
    } catch { Log "autostart: $($_.Exception.Message)" }
})
$null = $menu.Items.Add($loginItem)
$menu.Add_Opening({ $loginItem.Checked = (Test-Path $StartupLink) })
$null = $menu.Items.Add('Quit the helper', $null, { [Windows.Forms.Application]::Exit() })
$icon.ContextMenuStrip = $menu
$icon.Add_DoubleClick({ try { Open-PanelFromTray } catch { Log "open: $($_.Exception.Message)" } })
$icon.Visible = $true

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 150
$timer.Add_Tick({
    try {
        while ($listener.Pending()) { Serve ($listener.AcceptTcpClient()) }
        foreach ($j in $script:Jobs.Values) {
            if (-not $j.Told -and $j.Process.HasExited) {
                $j.Told = $true
                $code = $j.Process.ExitCode
                Log "job $($j.Id) $(if ($code -eq 0) { 'done' } else { "failed ($code)" })"
                if ($code -eq 0) { Notify "$($j.What): done." }
                else { Notify "$($j.What): failed. The panel shows what happened." 'Warning' }
            }
        }
    } catch { Log "tick: $($_.Exception.Message)" }
})
$timer.Start()
[Windows.Forms.Application]::Run()

$timer.Stop()
$icon.Visible = $false; $icon.Dispose()
$listener.Stop()
Log 'stopped'
$script:Mutex.ReleaseMutex()
