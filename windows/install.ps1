# UVMC installer for Windows 10/11, any edition (Multipass's HCS driver, on Windows's Virtual
# Machine Platform). Start it with install.cmd. Safe to run again: finished steps are skipped,
# an older Multipass is upgraded in place and its machines moved off the old Hyper-V driver.

$ErrorActionPreference = 'Stop'
$Name     = 'uvmc'
$Repo     = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $env:ProgramData 'UVMC'
$TaskName = 'UVMC install (continue after restart)'
. (Join-Path $PSScriptRoot 'timeouts.ps1')
. (Join-Path $PSScriptRoot 'multipass.ps1')   # $MultipassVersion, Find-Multipass, Wait-Multipass, ...

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
# Multipass straight from its GitHub release, checked against the pinned hash (multipass.ps1),
# with the download shown as it goes. curl.exe comes with Windows 10 (1803) and 11; it's much
# faster than PowerShell 5's Invoke-WebRequest, which is only the fallback.
function Install-MultipassDirect {
    $url = "https://github.com/canonical/multipass/releases/download/v$MultipassVersion/multipass-$MultipassVersion%2Bwin-win64.msi"
    $msi = Join-Path $env:TEMP "multipass-$MultipassVersion-win64.msi"
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    Write-Host "Downloading Multipass $MultipassVersion from GitHub (about 80 MB)..."
    $downloaded = $false
    for ($try = 1; $try -le 3 -and -not $downloaded; $try++) {
        Remove-Item -Force $msi -ErrorAction SilentlyContinue
        if (Test-Path $curl) {
            & $curl -L --fail --connect-timeout 30 --retry 2 --progress-bar -o $msi $url
            $downloaded = ($LASTEXITCODE -eq 0)
            if (-not $downloaded) { Write-Host "Download failed (curl code $LASTEXITCODE); try $try of 3." }
        } else {
            try {
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $ProgressPreference = 'SilentlyContinue'   # the progress bar makes it very slow in PowerShell 5
                Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
                $downloaded = $true
            } catch { Write-Host "Download failed ($($_.Exception.Message)); try $try of 3." }
        }
        if (-not $downloaded -and $try -lt 3) { Start-Sleep -Seconds 10 }
    }
    if (-not $downloaded) {
        Write-Host 'Multipass could not be downloaded. check-download.cmd (in the windows folder) finds out why.'
        return $null
    }
    if ((Get-FileHash -Algorithm SHA256 $msi).Hash -ne $MultipassSha256) {
        Remove-Item -Force $msi -ErrorAction SilentlyContinue
        Write-Host 'The downloaded file is not the expected Multipass installer (hash mismatch), so it was not run.'
        return $null
    }
    Write-Host "Installing Multipass $MultipassVersion..."
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
    Remove-Item -Force $msi -ErrorAction SilentlyContinue
    if ($p.ExitCode -notin 0, 3010) { Write-Host "The Multipass installer ended with code $($p.ExitCode)."; return $null }
    return Find-Multipass
}

# 1. Admin rights (asks once, then reopens this script elevated)
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

Disable-QuickEdit
try {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    # 2. Virtual Machine Platform: what Multipass's HCS driver runs on. Every edition of Windows
    #    10/11 has it, Home included (Hyper-V, which Multipass 1.16 needed, is Pro and up only).
    Step 'Checking Virtual Machine Platform'
    $vmp = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction SilentlyContinue
    if (-not $vmp) {
        Fail 'Virtual Machine Platform is not available on this PC (Windows 10 version 2004 or newer is needed).'
    }
    if ($vmp.State -ne 'Enabled') {
        if ($vmp.State -ne 'EnablePending') {
            Step 'Turning on Virtual Machine Platform (needs one restart)'
            Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All -NoRestart | Out-Null
        }
        $user      = "$env:USERDOMAIN\$env:USERNAME"
        $action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
        Write-Host 'Virtual Machine Platform is turned on. After the restart, the installer continues by itself.'
        if ((Read-Host 'Restart now? [Y/n]') -notmatch '^[nN]') { Restart-Computer -Force }
        exit
    }
    Write-Host 'Virtual Machine Platform is on.'

    # 3. Multipass: installed if missing, upgraded if older than the version UVMC is tested with.
    #    Its installer upgrades in place and keeps the machines (nothing to uninstall first).
    $mp = Find-Multipass
    $have = if ($mp) { Get-MultipassVersion $mp } else { $null }
    $pinned = [version]$MultipassVersion
    function Good-Enough {
        $m = Find-Multipass
        if (-not $m) { return $false }
        $v = Get-MultipassVersion $m
        return ($v -and $v -ge $pinned)
    }
    if (-not $mp -or ($have -and $have -lt $pinned)) {
        $upgrading = [bool]$mp
        if ($upgrading) {
            Step "Upgrading Multipass $have to $MultipassVersion (the UVMC machine is kept)"
            & (Join-Path $PSScriptRoot 'save-logs.cmd')
            $null = Invoke-Mp $mp @('stop', '--all')
            # A service stuck in "stopping" would keep Multipass's installer waiting: end it.
            $svc = Get-Service -Name Multipass -ErrorAction SilentlyContinue
            if ($svc -and $svc.Status -eq 'StopPending') {
                Get-Process multipassd -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
                Start-Sleep -Seconds 3
            }
        } else {
            Step 'Installing Multipass'
        }
        # 1) Downloaded from its GitHub release (winget doesn't list the pinned version yet).
        $null = Install-MultipassDirect
        # 2) winget, given at most 5 minutes (it can hang when GitHub's downloads misbehave).
        #    The same version as the direct download, so every PC gets the one UVMC is tested with.
        if (-not (Good-Enough) -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            Write-Host 'Trying winget instead.'
            $verb = if ($upgrading) { 'upgrade' } else { 'install' }
            $w = Start-Process winget.exe -ArgumentList "$verb -e --id Canonical.Multipass --version $MultipassVersion --silent --accept-package-agreements --accept-source-agreements" -NoNewWindow -PassThru
            if (-not $w.WaitForExit(300000)) {
                Write-Host 'winget is taking too long; stopping it.'
                Stop-Process -Id $w.Id -Force -ErrorAction SilentlyContinue
            }
        }
        $mp = Find-Multipass
        if (-not (Good-Enough)) {
            if ($upgrading -and $mp) {
                Write-Host "Multipass could not be upgraded; UVMC carries on with $have, which it no longer tests." -ForegroundColor Yellow
                Write-Host "Install $MultipassVersion by hand from https://canonical.com/multipass/install, then run install.cmd again." -ForegroundColor Yellow
            } else {
                Fail 'Multipass did not install. Install it by hand from https://canonical.com/multipass/install, then run install.cmd again.'
            }
        }
        if (-not $upgrading) { Set-Content -Path (Join-Path $StateDir 'installed-multipass') -Value 'yes' }
        # Multipass 1.17.0's installer, upgrading a PC whose storage was moved, writes
        # "local.driver=hyperv" to "<storage>multipassd.conf" (no backslash: next to the storage
        # folder, e.g. machinemultipassd.conf in the repo). Nothing reads it; take it away.
        $st = [Environment]::GetEnvironmentVariable('MULTIPASS_STORAGE', 'Machine')
        if ($st -and (Test-Path -LiteralPath "$($st.TrimEnd('\'))multipassd.conf")) {
            $stray = "$($st.TrimEnd('\'))multipassd.conf"
            if ((Get-Content -LiteralPath $stray -Raw) -match '^\s*local\.driver=\w+\s*$') { Remove-Item -LiteralPath $stray -Force }
        }
    } else {
        Write-Host "Multipass $have found: $mp"
    }

    # Tell the service its storage folder directly (see Sync-MultipassServiceStorage).
    $null = Sync-MultipassServiceStorage

    Step 'Waiting for Multipass to be ready'
    if (-not (Wait-Multipass $mp)) { if ($script:MultipassWaitError) { Fail $script:MultipassWaitError }; Fail 'Multipass is not responding. Restart the PC and run the installer again.' }
    # ...and again after each start of Windows (see Register-MultipassBootTask).
    try { Register-MultipassBootTask }
    catch {
        Write-Host "Could not add the task that starts Multipass after Windows starts: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor Yellow
        Write-Host 'UVMC works meanwhile; after a restart of Windows, Multipass may need starting by hand.' -ForegroundColor Yellow
    }

    # 3a. Multipass 1.17 deprecates its Hyper-V driver, and a PC that had 1.16 keeps it after the
    #     upgrade, so its machines are moved to the HCS driver (Multipass copies them, snapshots
    #     included, and keeps the originals until they're deleted below).
    if ((Get-MultipassDriver $mp) -eq 'hyperv') {
        Step "Moving Multipass's machines to its new HCS driver (copies each machine's disks; a few minutes)"
        if (-not (Switch-ToHcs $mp)) { Fail 'Moving to the HCS driver failed. Run install.cmd again; the machines stay on Hyper-V meanwhile.' }
        Write-Host 'Moved.'
        if (Test-Ok { & $mp info $Name }) {
            # The Hyper-V original is deleted only once the copy starts and answers.
            $null = Invoke-Mp $mp @('start', $Name)
            $null = Invoke-Mp $mp @('exec', $Name, '--', 'timeout', '-k', '5', '30', 'true')
            if ($LASTEXITCODE -eq 0) {
                Write-Host 'The UVMC machine works on the new driver. Its old Hyper-V copy is kept as a fallback until you remove it;'
                Write-Host 'it takes as much disk space as the machine itself.'
                if ((Read-Host 'Remove the old Hyper-V copy now? [Y/n]') -notmatch '^[nN]') {
                    if (Remove-HyperVOriginal $mp $Name) { Write-Host 'Removed.' }
                    else { Write-Host 'Could not remove it. uninstall.cmd removes it with the machine.' -ForegroundColor Yellow }
                    $null = Invoke-Mp $mp @('start', $Name)
                }
            } else {
                Write-Host 'The UVMC machine did not answer on the new driver; its Hyper-V copy is kept.' -ForegroundColor Yellow
            }
            if (Test-Path (Join-Path $env:LOCALAPPDATA 'UVMC\local-model.json')) {
                Write-Host 'The machine is on a new network now: run local-model once so the model on this PC follows it.' -ForegroundColor Yellow
            }
        }
    }

    # 3b. Where the machine is stored. You choose: next to UVMC (this repo's 'machine' folder,
    # ignored by git), Multipass's default (C:\ProgramData\Multipass), or another folder.
    # It's Multipass's own setting (MULTIPASS_STORAGE), so it's only offered while Multipass
    # has no other machines.
    $EnvKey  = 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment'
    $mpHome  = Get-MultipassStorage
    $Storage = $mpHome
    if (-not (Test-Ok { & $mp info $Name })) {
        $others = @()
        try { $others = @(((& $mp list --format json | Out-String) | ConvertFrom-Json).list | Where-Object { $_.name -ne $Name }) } catch {}
        if ($others.Count -gt 0) {
            Write-Host "Multipass already has other machines, so the UVMC machine goes where they are: $mpHome"
        } else {
            $nextTo = Join-Path $Repo 'machine'
            function Free-GB($path) {
                try { '{0:N0} GB free' -f ((Get-PSDrive (Split-Path -Qualifier $path).TrimEnd(':')).Free / 1GB) } catch { 'free space unknown' }
            }
            Step 'Where to store the UVMC machine'
            Write-Host 'It takes about 5 GB at first and can grow to about 16 GB.'
            Write-Host "  Enter      next to UVMC: $nextTo ($(Free-GB $nextTo))"
            Write-Host "  M          Multipass's usual place: $mpHome ($(Free-GB $mpHome))"
            Write-Host '  or type another folder, e.g. E:\VMs\uvmc (a new or empty folder)'
            while ($true) {
                $answer = (Read-Host 'Your choice').Trim().Trim('"')
                if ($answer -eq '') { $choice = $nextTo }
                elseif ($answer -match '^[mM]$') { $choice = $mpHome }
                else { $choice = $answer }
                if ($choice -notmatch '^[A-Za-z]:\\.+') { Write-Host 'Type a full folder path, like E:\VMs\uvmc (not a whole drive).'; continue }
                if (-not (Test-Path (Split-Path -Qualifier $choice))) { Write-Host "There is no drive $(Split-Path -Qualifier $choice)."; continue }
                if ($choice -ne $mpHome -and (Test-Path $choice) -and (Get-ChildItem -Force $choice | Select-Object -First 1)) {
                    Write-Host 'That folder is not empty. Pick a new or empty folder, so nothing else gets mixed in.'; Write-Host '(Just uninstalled UVMC? Restart Windows first: until then Multipass may still be using that folder. Do not empty it by hand.)'; continue
                }
                break
            }
            $choice = $choice.TrimEnd('\')
            if ($choice -ne $mpHome.TrimEnd('\')) {
                Write-Host "Storing the machine in $choice"
                Stop-Service Multipass
                New-Item -ItemType Directory -Force -Path $choice | Out-Null
                if (Test-Path $mpHome) { Copy-Item -Path (Join-Path $mpHome '*') -Destination $choice -Recurse -Force }
                Set-ItemProperty -Path $EnvKey -Name MULTIPASS_STORAGE -Value $choice
                # Remembered so uninstall can undo it; UVMC made this folder, so uninstall may delete it.
                Set-Content -Path (Join-Path $StateDir 'storage') -Value $choice
                $null = Sync-MultipassServiceStorage   # the service uses it now, not after a restart of Windows
                if (-not (Wait-Multipass $mp)) { Fail 'Multipass did not come back after moving its storage. Restart the PC and run the installer again.' }
                $Storage = $choice
            }
        }
    }

    # 4. The machine. Steps after creating it are remembered when they finish, so running the
    # installer again after an error carries on where it stopped.
    $DoneFile = Join-Path $StateDir 'setup-done'
    if (Test-Ok { & $mp info $Name }) {
        if (Test-Path $DoneFile) {
            Write-Host "The '$Name' machine already exists, keeping it. To start over, run uninstall.cmd first."
        } else {
            Write-Host "The '$Name' machine exists; finishing its setup."
            & $mp start $Name
        }
    } else {
        Step 'Creating the UVMC machine (downloads Ubuntu; about 5-10 minutes)'
        # 3 GB when this PC has 12 GB or more, else 2 GB (then the agent gets less). UVMC_MEMORY overrides.
        $vmMem = $env:UVMC_MEMORY
        if (-not $vmMem) {
            $ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
            $vmMem = if ($ramGB -ge 12) { '3G' } else { '2G' }
        }
        Write-Host "Memory for the machine: $vmMem"
        & $mp launch 24.04 --name $Name --cpus 2 --memory $vmMem --disk 15G --cloud-init (Join-Path $Repo 'cloud-init.yaml') --timeout 1800
        if ($LASTEXITCODE -ne 0) { Fail 'Creating the machine failed.' }
        $records = Join-Path $Storage 'data\hcs\vault\multipassd-instance-image-records.json'   # the HCS driver's records
        if ((Test-Path (Join-Path $StateDir 'storage')) -and -not (Select-String -Path $records -Pattern "`"$Name`"" -Quiet -ErrorAction SilentlyContinue)) {
            Write-Host "Note: the machine was not stored in $Storage (Multipass may need a restart of Windows to see the new location). It works anyway, from C:." -ForegroundColor Yellow
        }
    }

    if (-not (Test-Path $DoneFile)) {
        Step 'Finishing setup inside the machine'
        & $mp exec $Name -- sudo cloud-init status --wait | Out-Null
        & $mp exec $Name -- timeout -k 5 30 sudo test -d /home/agent/work
        $code = $LASTEXITCODE
        if (Confirm-VmTimeout 'install' 'sudo test -d /home/agent/work' 30 $code) { Fail 'The machine did not answer. Run install.cmd again to continue.' }
        if ($code -ne 0) {
            Fail "Setup inside the machine did not finish (the agent's work folder is missing). Details: multipass exec $Name -- sudo cat /var/log/cloud-init-output.log"
        }

        Step 'Installing the supervisor'
        & (Join-Path $PSScriptRoot 'update.cmd')
        if ($LASTEXITCODE -ne 0) { Fail 'Installing the supervisor failed.' }

        # 5. API keys and the Claude plan sign-in are done in the panel (Settings), not here:
        #    typed input piped into "multipass exec" hangs with Multipass on Windows (1.16, still in 1.17).

        # 6. Factory snapshot (kept if an earlier run already took it)
        if (-not (Test-Ok { & $mp info "$Name.clean" })) {
            Step 'Saving the clean snapshot (your factory reset point)'
            & $mp stop $Name
            & $mp snapshot $Name --name clean
            if ($LASTEXITCODE -ne 0) { Fail 'Taking the snapshot failed.' }
            & $mp start $Name
        }
        Set-Content -Path $DoneFile -Value 'yes'
    }

    # 6b. Claude apps on this PC (optional; connect-claude-apps does the same later)
    if (-not (Test-Path (Join-Path $StateDir 'asked-claude-apps'))) {
        Step 'Claude apps on this PC (optional)'
        Write-Host 'Claude Code and Claude Desktop on this PC can hand tasks to the UVMC agent, as a tool.'
        Write-Host 'Their tasks follow the same rules, and anything risky still waits for your approval in the panel.'
        $a = Read-Host 'Let the Claude apps on this PC use UVMC? You can change this later. [y/N]'
        Set-Content -Path (Join-Path $StateDir 'asked-claude-apps') -Value 'yes'
        if ($a -match '^[yY]') {
            & (Join-Path $PSScriptRoot 'connect-claude-apps.ps1') -NoPause -Yes
            # ...and switch it on in UVMC (Settings, "Claude apps on your PC").
            $on = $false
            for ($i = 0; $i -lt 30 -and -not $on; $i++) {
                $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                $r = & $mp exec $Name -- timeout -k 5 30 sudo uvmc-pc-apps on 2>$null
                $code = $LASTEXITCODE
                $ErrorActionPreference = $old
                $null = Confirm-VmTimeout 'install' 'sudo uvmc-pc-apps on' 30 $code   # tried again below
                if ("$r".Trim() -eq '200') { $on = $true } else { Start-Sleep -Seconds 2 }
            }
            if ($on) { Write-Host 'Switched on in UVMC.' -ForegroundColor Green }
            else { Write-Host 'Could not switch it on in UVMC yet: do it in the panel, Settings, "Claude apps on your PC".' -ForegroundColor Yellow }
            Set-Content -Path (Join-Path $StateDir 'connected-claude-apps') -Value 'yes'
        }
    }

    # 6c. The UVMC helper (optional; helper -AutoStart / -NoAutoStart does the same later)
    if (-not (Test-Path (Join-Path $StateDir 'asked-helper'))) {
        Step 'The UVMC helper (optional)'
        Write-Host 'A small program with a tray icon that lets the panel do what only this PC can do:'
        Write-Host 'download, start, stop and delete the models on your graphics card. It listens on this PC only.'
        # No unless you say yes: nothing of UVMC's starts with Windows by itself (the user, 2026-11-11).
        $a = Read-Host 'Start the UVMC tray icon each time you log in? The panel shortcut starts it anyway. [y/N]'
        Set-Content -Path (Join-Path $StateDir 'asked-helper') -Value 'yes'
        if ($a -match '^[yY]') { & (Join-Path $PSScriptRoot 'helper.ps1') -AutoStart }
        else { Write-Host 'Not at login. The UVMC Panel shortcut starts it when you open the panel (helper -AutoStart if you change your mind).' }
    }

    # 6d. Models on this PC's graphics card (optional; local-model does it later)
    if (-not (Test-Path (Join-Path $StateDir 'asked-local-model')) -and -not (Test-Path (Join-Path $env:LOCALAPPDATA 'UVMC\local-model.json'))) {
        Step 'Models on this PC (optional)'
        Write-Host "UVMC's agent can also run on your graphics card (NVIDIA, AMD, Intel), with no account and nothing to buy."
        Write-Host 'This downloads llama.cpp and a model sized to your card (several GB) in a window of its own.'
        $a = Read-Host 'Set up a model on this PC now? You can do it later with local-model. [y/N]'
        Set-Content -Path (Join-Path $StateDir 'asked-local-model') -Value 'yes'
        if ($a -match '^[yY]') {
            # In its own window, without administrator rights (it asks for them once, for the firewall rule).
            Start-Process explorer.exe -ArgumentList "`"$(Join-Path $PSScriptRoot 'local-model.cmd')`""
            Write-Host 'Started in its own window.'
        }
    }

    # 7. Shortcuts
    Step 'Adding shortcuts'
    $ws = New-Object -ComObject WScript.Shell
    $links = @(
        @{ Name = 'UVMC Panel.lnk'; Target = 'panel.cmd'; Text = 'Open the UVMC control panel in your browser' }
    )
    foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
        # Older installs had a 'UVMC' shortcut that opened Claude Code outside the supervisor.
        Remove-Item -Force (Join-Path $dir 'UVMC.lnk') -ErrorAction SilentlyContinue
        foreach ($l in $links) {
            $lnk = $ws.CreateShortcut((Join-Path $dir $l.Name))
            $lnk.TargetPath       = Join-Path $PSScriptRoot $l.Target
            $lnk.WorkingDirectory = $PSScriptRoot
            $lnk.Description      = $l.Text
            $lnk.Save()
        }
    }

    Step 'Done'
    Write-Host 'Open the control panel with the UVMC Panel shortcut. Then, in its Settings tab, add your API key or sign in with your Claude plan.'
    Read-Host 'Press Enter to close' | Out-Null
}
catch {
    # Where it happened too: Windows' own messages ("The parameter is incorrect") don't say.
    Fail ("$($_.Exception.Message)`n" + $_.InvocationInfo.PositionMessage)
}
