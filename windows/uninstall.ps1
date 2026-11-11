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
. (Join-Path $PSScriptRoot 'multipass.ps1')   # Find-Multipass, Remove-HyperVOriginal, ...

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

Disable-QuickEdit
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
                # Multipass on Windows reports an error after pulling a file (it can't give it Unix
                # permissions; canonical/multipass#4446) although the file arrived: the file counts.
                $null = Invoke-Mp $mp @('transfer', "${Name}:/tmp/uvmc-work.tgz", $file)
                $ok = (Test-Path -LiteralPath $file) -and ((Get-Item -LiteralPath $file).Length -gt 0)
                # ...and gives it the Unix permissions as Windows ones (no delete right): back to the folder's.
                if ($ok) { & icacls.exe $file /reset /q *> $null }
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
        # A machine Multipass moved off its old Hyper-V driver (1.17) may still have its original.
        if ((Get-MultipassDriver $mp) -eq 'hcs' -and (Test-HyperVOriginal $mp $Name)) {
            Step 'Deleting the old Hyper-V copy of the machine'
            if (-not (Remove-HyperVOriginal $mp $Name)) { Write-Host 'Could not delete it.' -ForegroundColor Yellow }
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
    Unregister-MultipassBootTask

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
                # Services get the system environment as it was when Windows started: a Multipass
                # installed before the next restart would still store its data in this folder.
                $restartNeeded = $true
                if ($storage -match '^[A-Za-z]:\\.+' -and (Test-Path $storage)) {   # a folder the installer made, never a drive
                    Remove-Item -Recurse -Force $storage -ErrorAction SilentlyContinue
                }
            }
        }
    }
    Remove-Item -Recurse -Force $StateDir -ErrorAction SilentlyContinue

    Step 'Done'
    Write-Host 'Virtual Machine Platform stays on (harmless when unused; WSL uses it too). To turn it off, run as administrator:'
    Write-Host '  Disable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform'
    Write-Host 'and restart. (UVMC before 0.21.1 turned on Hyper-V: Microsoft-Hyper-V-All, the same way.)'
    if ($restartNeeded) {
        Write-Host ''
        Write-Host 'Restart Windows before installing UVMC or Multipass again: until then, a new Multipass would' -ForegroundColor Yellow
        Write-Host "still put its data in the folder just removed ($storage)." -ForegroundColor Yellow
    }
    Read-Host 'Press Enter to close' | Out-Null
}
catch {
    Fail $_.Exception.Message
}
