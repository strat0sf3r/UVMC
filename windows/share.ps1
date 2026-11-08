# Shares the folders listed in shares.txt with UVMC (and stops sharing removed ones).
# Start it with share.cmd. -Quiet: no pause at the end (used by the panel launcher).
param([switch]$Quiet)

$ErrorActionPreference = 'Stop'
$Name   = 'uvmc'
$Repo   = Split-Path -Parent $PSScriptRoot
$List   = Join-Path $Repo 'shares.txt'
if (-not (Test-Path $List)) { Copy-Item (Join-Path $Repo 'shares.example.txt') $List -ErrorAction SilentlyContinue }
$Prefix = '/srv/uvmc-host/'

function Fail($text) {
    Write-Host "ERROR: $text" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}
function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}
function Native([scriptblock]$cmd) {
    # Runs a native command; returns its output as text (PowerShell 5.1 safe).
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $cmd 2>&1 | Out-String
    $script:code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return $out
}

$mp = Find-Multipass
if (-not $mp) { Fail 'Multipass is not installed. Run install.cmd first.' }
if (-not (Test-Path $List)) { Fail "shares.txt not found in $Repo" }

# 1. Read shares.txt
$shares = [ordered]@{}
foreach ($line in Get-Content $List) {
    $line = $line.Trim()
    if (-not $line -or $line.StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -ne 2) { Write-Host "Skipped (no '='): $line" -ForegroundColor Yellow; continue }
    $n = $parts[0].Trim()
    $p = $parts[1].Trim()
    if ($n -notmatch '^[a-z0-9][a-z0-9_-]{0,40}$') { Write-Host "Skipped '$n': use lowercase letters, digits, - and _" -ForegroundColor Yellow; continue }
    if (-not [IO.Path]::IsPathRooted($p)) { $p = Join-Path $Repo $p }
    if (-not (Test-Path $p -PathType Container)) { Write-Host "Skipped '$n': folder not found: $p" -ForegroundColor Yellow; continue }
    $full = (Resolve-Path $p).Path
    $home_ = [Environment]::GetFolderPath('UserProfile')
    if ($full.TrimEnd('\') -eq $home_.TrimEnd('\') -or $full -match '^[A-Za-z]:\\?$') {
        Write-Host "Skipped '$n': sharing a whole drive or your user folder is not allowed." -ForegroundColor Yellow; continue
    }
    $shares[$n] = $full
}

# 2. Windows needs folder sharing switched on in Multipass once (an admin prompt).
$setting = (Native { & $mp get local.privileged-mounts }).Trim()
if ($setting -notmatch 'true') {
    Write-Host 'Turning on folder sharing in Multipass (Windows asks for admin rights once)...'
    Start-Process -FilePath $mp -ArgumentList 'set', 'local.privileged-mounts=true' -Verb RunAs -Wait
}

# 3. Mount what's listed, unmount what's not anymore.
# (Changing that setting restarts Multipass's service, so give it a moment to come back.)
for ($i = 0; $i -lt 20; $i++) {
    $startOut = Native { & $mp start $Name }
    if ($script:code -eq 0) { break }
    Start-Sleep -Seconds 3
}
if ($script:code -ne 0) { Fail "Multipass could not start the UVMC machine. It said: $($startOut.Trim())" }
$json = Native { & $mp info $Name --format json }
$mounted = @()
try {
    $info = ($json | ConvertFrom-Json).info.$Name
    # Names of the current mounts; an empty "mounts" gives no names (not one empty name).
    if ($info.mounts) { $mounted = @($info.mounts.PSObject.Properties | ForEach-Object { $_.Name } | Where-Object { $_ }) }
} catch { }

foreach ($n in $shares.Keys) {
    $target = $Prefix + $n
    if ($mounted -contains $target) { Write-Host "Already shared: $n = $($shares[$n])"; continue }
    $out = Native { & $mp mount $shares[$n] "${Name}:$target" }
    if ($script:code -eq 0) { Write-Host "Shared: $n = $($shares[$n])" -ForegroundColor Green }
    else { Write-Host "Could not share $n`: $out" -ForegroundColor Red }
}
foreach ($m in $mounted) {
    if ($m -and $m.StartsWith($Prefix) -and -not $shares.Contains($m.Substring($Prefix.Length))) {
        Native { & $mp umount "${Name}:$m" } | Out-Null
        Write-Host "Stopped sharing: $($m.Substring($Prefix.Length))"
    }
}

# 4. Give the agent its read-only views.
Native { & $mp exec $Name -- sudo /usr/local/sbin/uvmc-shares-sync } | Out-Null
Write-Host ''
Write-Host 'Done. The agent sees these under /mnt/shared (read-only); changes need your approval in the panel.'
if (-not $Quiet) { Read-Host 'Press Enter to close' | Out-Null }
