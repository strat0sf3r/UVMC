# Time limits for quick commands sent to the UVMC machine, and a log of the ones that ran out.
# Dot-sourced by the other scripts: . (Join-Path $PSScriptRoot 'timeouts.ps1')
#
# Why: with Multipass 1.16 on Windows, "multipass exec" keeps a CPU core busy for as long as it
# runs whenever its input isn't a console window (the Claude apps' relay, scripts started without
# a window), and an exec the machine never answers never ends. Quick commands therefore get
# "timeout" inside the machine, which stops the command and everything it started, so the exec
# ends. The hidden lookups (Invoke-VmQuick) also get a limit here on the PC, for when Multipass
# itself doesn't answer. Each timeout adds a line to %LOCALAPPDATA%\UVMC\timeouts.log
# (the newest 500 are kept); diagnose shows the last ones.

$script:TimeoutLog = Join-Path $env:LOCALAPPDATA 'UVMC\timeouts.log'

function Write-TimeoutLog([string]$Who, [string]$What, [string]$How) {
    # Never fails the script that calls it.
    try {
        $dir = Split-Path -Parent $script:TimeoutLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        $line = '{0}  {1}  {2}  {3}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Who, $What, $How
        $keep = @()
        if (Test-Path $script:TimeoutLog) { $keep = @(Get-Content -Path $script:TimeoutLog -Tail 499 -Encoding UTF8) }
        Set-Content -Path $script:TimeoutLog -Value ($keep + $line) -Encoding UTF8
    } catch {}
}

# "timeout" ends with 124 when it stopped the command (137 when it had to kill it).
function Test-VmTimedOut([int]$Code) { return ($Code -eq 124 -or $Code -eq 137) }

# For the scripts that run in a window: after "& $mp exec ... -- timeout -k 5 <limit> ...",
# logs a timeout and says so. Returns $true if the command was stopped.
function Confirm-VmTimeout([string]$Who, [string]$What, [int]$Limit, [int]$Code) {
    if (-not (Test-VmTimedOut $Code)) { return $false }
    Write-TimeoutLog $Who $What "stopped in the VM after $Limit s"
    Write-Host "The UVMC machine did not answer within $Limit seconds ($What); the command was stopped." -ForegroundColor Yellow
    return $true
}

function Format-NativeArg([string]$a) {
    # Quotes one argument the way Windows programs split their command line.
    if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
    $s = $a -replace '(\\*)"', '$1$1\"'
    $s = $s -replace '(\\+)$', '$1$1'
    return '"' + $s + '"'
}

# Runs a quick command in the machine with a limit on both sides: "timeout" inside the machine,
# and on this PC the Multipass client is stopped if it hasn't returned 10 s after that.
# Returns @{ Out = its output; Code = exit code; TimedOut = $null, 'vm' or 'pc'; Message = for the user }.
# Writes nothing to the screen (the relay's output must stay pure JSON).
function Invoke-VmQuick([string]$Mp, [string]$Name, [int]$Limit, [string[]]$Command, [string]$Who) {
    $what = $Command -join ' '
    $all = @('exec', $Name, '--', 'timeout', '-k', '5', "$Limit") + $Command
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Mp
    $psi.Arguments = ($all | ForEach-Object { Format-NativeArg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $false   # share this script's console, if it has one (no new window)
    # Its input closed at once: the quick commands read none, "multipass exec" hangs on a hidden
    # console's input, and in the relay this script's input is the Claude app's messages.
    $psi.RedirectStandardInput = $true
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $out = $p.StandardOutput.ReadToEndAsync()
    $null = $p.StandardError.ReadToEndAsync()
    $pcLimit = $Limit + 10
    if (-not $p.WaitForExit($pcLimit * 1000)) {
        try { $p.Kill() } catch {}
        Write-TimeoutLog $Who $what "no answer from Multipass, client stopped after $pcLimit s"
        return @{ Out = ''; Code = -1; TimedOut = 'pc'
                  Message = "Multipass did not answer within $pcLimit seconds ($what); it was stopped." }
    }
    $p.WaitForExit()   # lets the output finish
    $code = $p.ExitCode
    if (Test-VmTimedOut $code) {
        Write-TimeoutLog $Who $what "stopped in the VM after $Limit s"
        return @{ Out = ''; Code = $code; TimedOut = 'vm'
                  Message = "The UVMC machine did not answer within $Limit seconds ($what); the command was stopped." }
    }
    return @{ Out = $out.Result; Code = $code; TimedOut = $null; Message = '' }
}
