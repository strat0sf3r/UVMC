# UVMC uninstaller for Windows. Start it with uninstall.cmd.

$ErrorActionPreference = 'Stop'
$Name     = 'uvmc'
$StateDir = Join-Path $env:ProgramData 'UVMC'
$TaskName = 'UVMC install (continue after restart)'

function Step($text) { Write-Host ''; Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ''
    Write-Host "ERROR: $text" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}
function Test-Ok([scriptblock]$cmd) {
    # Runs a native command quietly; true if it exits with 0.
    # (Windows PowerShell 5.1 would turn its error output into exceptions otherwise.)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $cmd *> $null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    return ($code -eq 0)
}
function Find-Multipass {
    $c = Get-Command multipass.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Multipass\bin\multipass.exe'
    if (Test-Path $p) { return $p }
    return $null
}

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

try {
    Write-Host 'This removes the UVMC machine (everything inside it and all its snapshots) and its shortcuts.'
    Write-Host 'This folder (the repo) is not touched.'
    if ((Read-Host 'Continue? [y/N]') -notmatch '^[yY]') { exit }

    $mp = Find-Multipass
    if ($mp) {
        if (Test-Ok { & $mp info $Name }) {
            if ((Read-Host "Save the agent's work folder to your Documents first? [Y/n]") -notmatch '^[nN]') {
                Step 'Saving the work folder'
                $docs = [Environment]::GetFolderPath('MyDocuments')
                $file = 'uvmc-work-{0}.tgz' -f (Get-Date -Format 'yyyyMMdd-HHmm')
                & $mp start $Name
                & $mp exec $Name -- sudo tar -czf /tmp/uvmc-work.tgz -C /home/agent work
                Push-Location $docs
                & $mp transfer "${Name}:/tmp/uvmc-work.tgz" $file
                $ok = ($LASTEXITCODE -eq 0)
                Pop-Location
                if ($ok) {
                    Write-Host "Saved to $(Join-Path $docs $file) (open with: tar -xzf $file)"
                } elseif ((Read-Host 'Saving failed. Delete the machine anyway? [y/N]') -notmatch '^[yY]') {
                    exit
                }
            }
            Step 'Saving run logs to the repo logs folder'
            & (Join-Path $PSScriptRoot 'save-logs.cmd')
            Step 'Deleting the machine and its snapshots'
            & $mp delete --purge $Name
            if ($LASTEXITCODE -ne 0) { Fail 'Deleting the machine failed.' }
        } else {
            Write-Host 'No UVMC machine found.'
        }
    }

    Step 'Removing the UVMC helper'
    try {
        & (Join-Path $PSScriptRoot 'helper.ps1') -Stop
        & (Join-Path $PSScriptRoot 'helper.ps1') -NoAutoStart
    } catch { Write-Host "Skipped: $($_.Exception.Message)" }
    $data = Join-Path $env:LOCALAPPDATA 'UVMC'
    foreach ($f in 'helper.json', 'helper.log', 'helper-jobs') { Remove-Item -Recurse -Force (Join-Path $data $f) -ErrorAction SilentlyContinue }

    if (Test-Path (Join-Path $data 'local-model.json')) {
        Write-Host ''
        Write-Host 'The model on this PC (llama.cpp, the downloaded models, its firewall rules) is still there.'
        if ((Read-Host 'Remove it too? This deletes the downloaded models. [y/N]') -match '^[yY]') {
            Step 'Removing the model on this PC'
            try { & (Join-Path $PSScriptRoot 'local-model.ps1') -Remove -Yes } catch { Write-Host "Skipped: $($_.Exception.Message)" }
        } else { Write-Host 'Kept. uninstall-local-model removes it later.' }
    }

    Step 'Taking UVMC out of the Claude apps on this PC'
    try { & (Join-Path $PSScriptRoot 'connect-claude-apps.ps1') -Remove -NoPause } catch { Write-Host "Skipped: $($_.Exception.Message)" }

    Step 'Removing shortcuts'
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        Remove-Item -Force (Join-Path $dir 'UVMC.lnk') -ErrorAction SilentlyContinue
        Remove-Item -Force (Join-Path $dir 'UVMC Panel.lnk') -ErrorAction SilentlyContinue
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    if ($mp -and (Test-Path (Join-Path $StateDir 'installed-multipass'))) {
        if ((Read-Host 'Multipass was installed by UVMC. Remove it too? [Y/n]') -notmatch '^[nN]') {
            Step 'Removing Multipass'
            winget uninstall -e --id Canonical.Multipass --silent
            if (Find-Multipass) {
                # Installed without winget (e.g. from a downloaded file): remove it through Windows.
                $apps = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
                    Where-Object { $_.DisplayName -like 'Multipass*' -and $_.PSChildName -like '{*}' }
                foreach ($a in $apps) { Start-Process msiexec.exe -ArgumentList "/x $($a.PSChildName) /qn /norestart" -Wait }
            }
            Remove-Item -Recurse -Force (Join-Path $env:ProgramData 'Multipass') -ErrorAction SilentlyContinue
            # The machine folder next to UVMC, if the installer put Multipass's storage there.
            $storageFile = Join-Path $StateDir 'storage'
            if (Test-Path $storageFile) {
                $storage = (Get-Content $storageFile -Raw).Trim()
                Remove-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment' -Name MULTIPASS_STORAGE -ErrorAction SilentlyContinue
                if ($storage -match '^[A-Za-z]:\\.+' -and (Test-Path $storage)) {   # a folder the installer made, never a drive
                    Remove-Item -Recurse -Force $storage -ErrorAction SilentlyContinue
                }
            }
        }
    }
    Remove-Item -Recurse -Force $StateDir -ErrorAction SilentlyContinue

    Step 'Done'
    Write-Host 'Hyper-V stays on (harmless when unused). To turn it off, run as administrator:'
    Write-Host '  Disable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All'
    Write-Host 'and restart.'
    Read-Host 'Press Enter to close' | Out-Null
}
catch {
    Fail $_.Exception.Message
}
