# Multipass on this PC: the version UVMC is tested with, and the steps the installer, update
# and uninstall share. Dot-source it: . (Join-Path $PSScriptRoot 'multipass.ps1')

# Pinned to the version UVMC is tested with. Update the version and hash together, from the
# GitHub release (the asset's sha256 digest) or winget-pkgs (manifests/c/Canonical/Multipass).
# winget doesn't list 1.17.0 yet (2026-11-10), so the installer downloads it directly first.
$MultipassVersion = '1.17.0'
$MultipassSha256  = '0EFD5C9ACCB876A7DFBAF01669AB40ED0D95435E6CBEA8F78CC5BDE351D481E5'

# Turns off QuickEdit for this window only (not the Windows setting): with it on, a click in
# the window starts selecting text and freezes the script's output until a key is pressed,
# which looks like a hang during the long steps (seen in uninstall, 2026-11-10).
function Disable-QuickEdit {
    try {
        if (-not ('UvmcConsole' -as [type])) {
            Add-Type -Namespace '' -Name UvmcConsole -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool GetConsoleMode(IntPtr h, out uint m);
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool SetConsoleMode(IntPtr h, uint m);
'@
        }
        $h = [UvmcConsole]::GetStdHandle(-10)   # the window's input
        $mode = [uint32]0
        if ([UvmcConsole]::GetConsoleMode($h, [ref]$mode)) {
            # ENABLE_QUICK_EDIT_MODE 0x40 off; ENABLE_EXTENDED_FLAGS 0x80 on, so the change applies.
            [void][UvmcConsole]::SetConsoleMode($h, (($mode -band (-bnot [uint32]0x40)) -bor [uint32]0x80))
        }
    } catch {}
}

function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}

# Runs a native command and returns its output as text, without Windows PowerShell 5.1 turning
# its error output into exceptions; $LASTEXITCODE is kept.
function Invoke-Mp([string]$mp, [string[]]$mpArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = (& $mp @mpArgs 2>&1 | Out-String)
    $ErrorActionPreference = $old
    return $out
}

# "1.17.0+win" -> [version]1.17.0; $null if Multipass doesn't answer.
function Get-MultipassVersion([string]$mp) {
    $out = Invoke-Mp $mp @('version')
    if ($out -match '(?m)^multipass\s+(\d+\.\d+\.\d+)') { return [version]$Matches[1] }
    return $null
}

# Waits until the Multipass service answers. Multipass 1.17.0 on Windows 10: the first start on
# the HCS driver creates a network per zone (three) and then looks its adapter up by a name
# Windows 10 has cut short, so the service crashes once per network it creates, and Windows
# restarts it only so many times. Each crash leaves its network made, so starting it again gets
# further; after three, it stays up (found 2026-11-10). Starting a service needs administrator
# rights, which the installer has.
function Wait-Multipass([string]$mp, [int]$tries = 8) {
    $script:MultipassWaitError = ''
    for ($i = 0; $i -lt $tries; $i++) {
        $out = Invoke-Mp $mp @('wait-ready', '--timeout', '30')
        if ($LASTEXITCODE -eq 0) { return $true }
        # Multipass trusts the users its installer set up, in its storage folder at that moment.
        # If the storage changed since (MULTIPASS_STORAGE before a restart of Windows), nobody is
        # trusted: waiting won't help (found 2026-11-10).
        if ($out -match 'not authenticated') {
            $script:MultipassWaitError = 'Multipass does not accept this user ("not authenticated"). Restart Windows, ' +
                'uninstall Multipass in Settings > Apps, then run install.cmd again: it installs Multipass afresh.'
            return $false
        }
        $svc = Get-Service -Name Multipass -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -eq 'Stopped') {
            try { Start-Service -Name Multipass -ErrorAction Stop } catch {}
        }
        Start-Sleep -Seconds 3
    }
    return $false
}

# Where Multipass keeps its machines, told to its service directly. The system-wide
# MULTIPASS_STORAGE reaches a service only after Windows restarts (services get the environment
# Windows started with), so a changed or removed folder was silently ignored until then: the
# machine was made in the old folder and seemed gone after the restart, and a Multipass
# reinstalled before a restart kept using the removed folder (both found 2026-11-10). The
# service's own Environment value is read each time the service starts, and it wins.
$MultipassServiceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\Multipass'
function Get-MultipassStorage {
    $st = [Environment]::GetEnvironmentVariable('MULTIPASS_STORAGE', 'Machine')
    if ($st) { return $st.TrimEnd('\') }
    return (Join-Path $env:ProgramData 'Multipass')
}
# Makes the service use Get-MultipassStorage; restarts it if that changed anything. Needs
# administrator rights. Returns $true when the service was restarted.
function Sync-MultipassServiceStorage {
    if (-not (Test-Path $MultipassServiceKey)) { return $false }
    $want = @("MULTIPASS_STORAGE=$(Get-MultipassStorage)")
    $have = @((Get-ItemProperty -Path $MultipassServiceKey -Name Environment -ErrorAction SilentlyContinue).Environment)
    if (($have -join "`n") -eq ($want -join "`n")) { return $false }
    New-ItemProperty -Path $MultipassServiceKey -Name Environment -PropertyType MultiString -Value $want -Force | Out-Null
    $svc = Get-Service -Name Multipass -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne 'Stopped') { Stop-Service -Name Multipass -Force -ErrorAction SilentlyContinue }
    Start-Service -Name Multipass -ErrorAction SilentlyContinue
    return $true
}

function Get-MultipassDriver([string]$mp) {
    $out = Invoke-Mp $mp @('get', 'local.driver')
    if ($LASTEXITCODE -ne 0) { return '' }
    # The Hyper-V driver also prints a warning that it's deprecated; the driver's name comes first.
    return ($out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
}

# Moves Multipass from the Hyper-V driver (deprecated in 1.17) to the HCS one. Multipass copies
# every machine across and keeps the Hyper-V originals; returns $true once it runs on HCS.
function Switch-ToHcs([string]$mp) {
    if ((Get-MultipassDriver $mp) -ne 'hyperv') { return $true }
    $null = Invoke-Mp $mp @('stop', '--all')
    $out = Invoke-Mp $mp @('set', 'local.driver=hcs')
    if ($LASTEXITCODE -ne 0) { Write-Host $out; return $false }
    if (-not (Wait-Multipass $mp)) { return $false }
    return ((Get-MultipassDriver $mp) -eq 'hcs')
}

# Deletes the Hyper-V original of a machine Multipass moved to HCS (it shares the copy's
# identity and network address, and takes the disk space of a second machine). Multipass only
# shows the originals while it's on the Hyper-V driver, so it switches there and back; the HCS
# copy isn't touched (tried 2026-11-10: copy and its snapshots kept, nothing migrated again).
function Remove-HyperVOriginal([string]$mp, [string]$name) {
    if ((Get-MultipassDriver $mp) -ne 'hcs') { return $false }
    $null = Invoke-Mp $mp @('stop', '--all')
    $null = Invoke-Mp $mp @('set', 'local.driver=hyperv')
    if ($LASTEXITCODE -ne 0) { return $false }
    $ok = $false
    if (Wait-Multipass $mp) {
        $null = Invoke-Mp $mp @('info', $name)
        if ($LASTEXITCODE -eq 0) {
            $null = Invoke-Mp $mp @('delete', '--purge', $name)
            $ok = ($LASTEXITCODE -eq 0)
        } else { $ok = $true }   # no original left
    }
    $null = Invoke-Mp $mp @('set', 'local.driver=hcs')
    $null = Wait-Multipass $mp
    return ($ok -and (Get-MultipassDriver $mp) -eq 'hcs')
}

# True if Multipass still keeps a Hyper-V original of this machine (only asked while on HCS).
function Test-HyperVOriginal([string]$mp, [string]$name) {
    $records = Join-Path (Get-MultipassStorage) 'data\vault\multipassd-instance-image-records.json'
    try { return [bool](Select-String -Path $records -Pattern "`"$name`"" -Quiet -ErrorAction Stop) } catch { return $false }
}
