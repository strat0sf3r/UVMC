# UVMC installer for Windows (Pro, Enterprise or Education: uses Hyper-V).
# Start it with install.cmd. Safe to run again: finished steps are skipped.

$ErrorActionPreference = 'Stop'
$Name     = 'uvmc'
$Repo     = Split-Path -Parent $PSScriptRoot
$StateDir = Join-Path $env:ProgramData 'UVMC'
$TaskName = 'UVMC install (continue after restart)'
. (Join-Path $PSScriptRoot 'timeouts.ps1')

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

# Multipass straight from its GitHub release, checked against the hash winget lists for it.
# (Update the version and hash together, from winget-pkgs: manifests/c/Canonical/Multipass.)
# Pinned to the version UVMC is tested with; winget uses it too. 1.17.0 (2026-10-19) is out and
# the upgrade is planned (docs/design.md, Planned work, "Multipass 1.17"): check for a newer
# release before each piece of work, and move this pin up once it's tested.
$MultipassVersion = '1.16.4'
$MultipassSha256  = 'B0C417FB8254FA61E4AA63895F59354BB3717C58100816651A9300DD98124AEA'
function Install-MultipassDirect {
    $url = "https://github.com/canonical/multipass/releases/download/v$MultipassVersion/multipass-$MultipassVersion%2Bwin-win64.msi"
    $msi = Join-Path $env:TEMP "multipass-$MultipassVersion-win64.msi"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $downloaded = $false
    for ($try = 1; $try -le 3 -and -not $downloaded; $try++) {
        try {
            $ProgressPreference = 'SilentlyContinue'   # the progress bar makes downloads very slow in PowerShell 5
            Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
            $downloaded = $true
        } catch {
            Write-Host "Download failed ($($_.Exception.Message)); try $try of 3."
            if ($try -lt 3) { Start-Sleep -Seconds 15 }
        }
    }
    $mine = $true   # a file we downloaded (deleted afterwards), not one of yours
    if ($downloaded) {
        if ((Get-FileHash -Algorithm SHA256 $msi).Hash -ne $MultipassSha256) {
            Remove-Item -Force $msi -ErrorAction SilentlyContinue
            Write-Host 'The downloaded file is not the expected Multipass installer (hash mismatch), so it was not run.'
            return $null
        }
    } else {
        # GitHub's download servers sometimes refuse programs in a region while browsers still
        # get through: use an installer you downloaded in your browser, if it's genuine.
        $msi = Find-DownloadedMultipass
        if (-not $msi) {
            Write-Host 'Tip: download Multipass for Windows in your browser (https://canonical.com/multipass/install),'
            Write-Host 'leave it in your Downloads folder, and run install.cmd again: it will use that file.'
            return $null
        }
        Write-Host "Using the installer from your Downloads folder: $msi"
        $mine = $false
    }
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
    if ($mine) { Remove-Item -Force $msi -ErrorAction SilentlyContinue }
    if ($p.ExitCode -notin 0, 3010) { Write-Host "The Multipass installer ended with code $($p.ExitCode)."; return $null }
    return Find-Multipass
}

# A Multipass installer in your Downloads folder: either exactly the version above, or any
# version signed by Canonical (Windows checks the signature).
function Find-DownloadedMultipass {
    $dl = Join-Path $env:USERPROFILE 'Downloads'
    $files = Get-ChildItem -Path $dl -Filter 'multipass*.msi' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    foreach ($f in $files) {
        if ((Get-FileHash -Algorithm SHA256 $f.FullName).Hash -eq $MultipassSha256) { return $f.FullName }
        $sig = Get-AuthenticodeSignature $f.FullName
        if ($sig.Status -eq 'Valid' -and $sig.SignerCertificate.Subject -match 'Canonical') { return $f.FullName }
        Write-Host "Skipping $($f.Name): not a genuine Multipass installer (no valid Canonical signature)."
    }
    return $null
}

# 1. Admin rights (asks once, then reopens this script elevated)
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

try {
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    # 2. Hyper-V
    Step 'Checking Hyper-V'
    $hv = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction SilentlyContinue
    if (-not $hv) {
        Fail 'Hyper-V is not available on this PC. It needs Windows Pro, Enterprise or Education.'
    }
    if ($hv.State -ne 'Enabled') {
        if ($hv.State -ne 'EnablePending') {
            Step 'Turning on Hyper-V (needs one restart)'
            Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -All -NoRestart | Out-Null
        }
        $user      = "$env:USERDOMAIN\$env:USERNAME"
        $action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
        Write-Host 'Hyper-V is turned on. After the restart, the installer continues by itself.'
        if ((Read-Host 'Restart now? [Y/n]') -notmatch '^[nN]') { Restart-Computer -Force }
        exit
    }
    Write-Host 'Hyper-V is on.'

    # 3. Multipass
    $mp = Find-Multipass
    if (-not $mp) {
        Step 'Installing Multipass'
        # 1) An installer you already downloaded in your browser (genuine ones only).
        $local = Find-DownloadedMultipass
        if ($local) {
            Write-Host "Using the installer from your Downloads folder: $local"
            $p = Start-Process msiexec.exe -ArgumentList "/i `"$local`" /qn /norestart" -Wait -PassThru
            if ($p.ExitCode -notin 0, 3010) { Write-Host "The Multipass installer ended with code $($p.ExitCode)." }
            $mp = Find-Multipass
        }
        # 2) winget, given at most 5 minutes (it can hang when GitHub's downloads misbehave).
        #    The same version as the direct download, so every PC gets the one UVMC is tested with.
        if (-not $mp -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            $w = Start-Process winget.exe -ArgumentList "install -e --id Canonical.Multipass --version $MultipassVersion --silent --accept-package-agreements --accept-source-agreements" -NoNewWindow -PassThru
            if (-not $w.WaitForExit(300000)) {
                Write-Host 'winget is taking too long; stopping it.'
                Stop-Process -Id $w.Id -Force -ErrorAction SilentlyContinue
            }
            $mp = Find-Multipass
        }
        # 3) Downloading the installer directly.
        if (-not $mp) {
            Write-Host 'winget could not install Multipass; downloading it directly instead.'
            $mp = Install-MultipassDirect
        }
        if (-not $mp) { Fail 'Multipass did not install. Install it by hand from https://canonical.com/multipass/install, then run install.cmd again.' }
        Set-Content -Path (Join-Path $StateDir 'installed-multipass') -Value 'yes'
    } else {
        Write-Host "Multipass found: $mp"
    }

    Step 'Waiting for Multipass to be ready'
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Ok { & $mp list }) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $ready) { Fail 'Multipass is not responding. Restart the PC and run the installer again.' }

    # 3b. Where the machine is stored. You choose: next to UVMC (this repo's 'machine' folder,
    # ignored by git), Multipass's default (C:\ProgramData\Multipass), or another folder.
    # It's Multipass's own setting (MULTIPASS_STORAGE), so it's only offered while Multipass
    # has no other machines.
    $EnvKey  = 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment'
    $current = (Get-ItemProperty -Path $EnvKey -Name MULTIPASS_STORAGE -ErrorAction SilentlyContinue).MULTIPASS_STORAGE
    $mpHome  = if ($current) { $current } else { Join-Path $env:ProgramData 'Multipass' }
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
                    Write-Host 'That folder is not empty. Pick a new or empty folder, so nothing else gets mixed in.'; continue
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
                Start-Service Multipass
                $ready = $false
                for ($i = 0; $i -lt 60; $i++) {
                    if (Test-Ok { & $mp list }) { $ready = $true; break }
                    Start-Sleep -Seconds 3
                }
                if (-not $ready) { Fail 'Multipass did not come back after moving its storage. Restart the PC and run the installer again.' }
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
        $records = Join-Path $Storage 'data\vault\multipassd-instance-image-records.json'
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
        #    typed input piped into "multipass exec" hangs with Multipass 1.16 on Windows.

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
    Fail $_.Exception.Message
}
