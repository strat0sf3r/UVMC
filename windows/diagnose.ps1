# diagnose: collects what is needed to find out why the plan sign-in or the Claude apps
# connection doesn't work, into diagnose.txt in the UVMC folder. Read-only: it changes nothing
# (except a line in the timeout log if the machine doesn't answer a command in time).
# Tokens are masked: the file shows no secret.
$ErrorActionPreference = 'Continue'
$Repo = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Repo 'diagnose.txt'
$lines = New-Object System.Collections.Generic.List[string]
function Add($text) { $lines.Add([string]$text); Write-Host $text }
function Section($title) { Add ''; Add "===== $title =====" }
function Run($title, [scriptblock]$block) {
    Section $title
    try { (& $block 2>&1 | Out-String -Width 400).TrimEnd() -split "`r?`n" | ForEach-Object { Add $_ } }
    catch { Add "FAILED: $_" }
}

$mp = (Get-Command multipass.exe -ErrorAction SilentlyContinue).Source
if (-not $mp) { $mp = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe' }
. (Join-Path $PSScriptRoot 'timeouts.ps1')
# Notes a command the machine didn't answer in time (in the report and in the timeout log).
function TimedOutNote([string]$what, [int]$limit) {
    if (Test-VmTimedOut $LASTEXITCODE) {
        Write-TimeoutLog 'diagnose' $what "stopped in the VM after $limit s"
        "(the machine did not answer within $limit s; stopped)"
    }
}
# Runs a command in the machine, stopped after 60 s if it doesn't answer.
function InVmCmd {
    & $mp exec uvmc -- timeout -k 5 60 @args
    TimedOutNote ($args -join ' ') 60
}
# Runs a bash script in the machine. Passed as base64: PowerShell 5 mangles quotes in native arguments.
function InVm($script) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script -replace "`r", '')))
    # The script runs from a file with no input, so nothing in it can wait for input; 60 s at most.
    & $mp exec uvmc -- timeout -k 5 70 bash -c "echo $b64 | base64 -d > /tmp/uvmc-diagnose.sh && timeout 60 bash /tmp/uvmc-diagnose.sh < /dev/null"
    TimedOutNote 'a diagnose script' 60
}
Add "UVMC diagnose, $(Get-Date -Format s)"

Run 'machine' { & $mp list }
Run 'supervisor version and status' { InVmCmd bash -c 'cat /opt/uvmc/uvmc/__init__.py | grep version; systemctl is-active uvmc; /usr/local/bin/claude --version; ls -la /usr/local/bin/claude' }
Run 'stored keys (names only)' { InVmCmd sudo ls -la /etc/uvmc/keys }
Run 'can the supervisor reach Claude' { InVm 'for u in https://platform.claude.com https://claude.ai https://api.anthropic.com; do printf "%s -> " $u; curl -s -o /dev/null -m 15 -w "%{http_code}\n" $u || echo failed; done' }
Run 'panel settings for the Claude apps' { InVm 'curl -s -H "Authorization: Bearer $(sudo cat /etc/uvmc/panel_token)" http://127.0.0.1:8080/api/settings/mcp; echo' }
Run 'supervisor log (last lines)' { InVmCmd sudo journalctl -u uvmc -n 300 --no-pager | Where-Object { $_ -notmatch 'pam_unix|uvmc-agent.slice|checkpoints|COMMAND=/usr/bin/cat|COMMAND=/usr/bin/stat|uvmc-shares-sync' } | ForEach-Object { if ($_.Length -gt 600) { $_.Substring(0, 600) } else { $_ } } }
Run 'the last plan sign-in' { InVmCmd sudo ls -la /var/lib/uvmc/signin; InVmCmd sudo cat /var/lib/uvmc/signin/last.txt }
Section 'commands the machine did not answer in time (last 20)'
if (Test-Path $script:TimeoutLog) { Get-Content $script:TimeoutLog -Tail 20 -Encoding UTF8 | ForEach-Object { Add $_ } }
else { Add 'none recorded' }

# --- the PC side of the Claude apps ---
Run 'Claude Code on this PC' {
    $c = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -match '\.(exe|cmd|bat)$' } | Select-Object -First 1
    if (-not $c) { 'claude is not on PATH' } else { $c.Source; & $c.Source --version; & $c.Source mcp get uvmc }
}
Section 'Claude Desktop config (only the uvmc entry)'
$cfgs = @(Join-Path $env:APPDATA 'Claude\claude_desktop_config.json')
$cfgs += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json' }
foreach ($f in $cfgs) {
    if (Test-Path $f) {
        Add "$f"
        try { $j = Get-Content $f -Raw | ConvertFrom-Json; Add (($j.mcpServers.uvmc | ConvertTo-Json -Depth 5)) ; Add ("servers: " + (($j.mcpServers.PSObject.Properties.Name) -join ', ')) }
        catch { Add "could not read it: $_" }
    } else { Add "$f (not there)" }
}
Run 'Claude apps running' { Get-Process -Name claude* -ErrorAction SilentlyContinue | Select-Object -Unique Name, Path | Format-Table -AutoSize }
Section 'Claude Desktop log for uvmc (last 60 lines)'
$logs = @(Join-Path $env:APPDATA 'Claude\logs')
$logs += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\logs' }
foreach ($d in $logs) {
    foreach ($n in 'mcp-server-uvmc.log', 'mcp.log') {
        $f = Join-Path $d $n
        if (Test-Path $f) { Add "--- $f"; Get-Content $f -Tail 60 | ForEach-Object { Add $_ } }
    }
}
Run 'the relay by hand (initialize)' {
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"diagnose","version":"1"}}}' |
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'uvmc.ps1') mcp
}

# Mask anything that looks like a token before saving.
$text = ($lines -join "`r`n")
$text = [regex]::Replace($text, 'sk-ant-[A-Za-z0-9_-]{6,}', { param($m) $m.Value.Substring(0, 14) + '...(masked)' })
$text = [regex]::Replace($text, '(Bearer\s+)[A-Za-z0-9]{20,}', '$1(masked)')
[IO.File]::WriteAllText($Out, $text, (New-Object Text.UTF8Encoding $false))
Write-Host ''
Write-Host "Saved: $Out" -ForegroundColor Green
