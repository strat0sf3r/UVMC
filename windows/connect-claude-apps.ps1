# Lets the Claude apps on this PC use UVMC as a tool (MCP): Claude Code and Claude Desktop.
# Start it with connect-claude-apps.cmd. -Remove takes UVMC out of both again.
# Claude Code starts "uvmc mcp" (windows\uvmc.ps1); Claude Desktop gets the UVMC extension
# (extension\uvmc.mcpb). Both find the machine by themselves, so nothing goes stale when its
# address changes. UVMC must also allow it: panel, Settings,
# "Claude apps on your PC".
param([switch]$Remove, [switch]$NoPause, [switch]$Yes)

$ErrorActionPreference = 'Stop'
$Relay = Join-Path $PSScriptRoot 'uvmc.ps1'
$Args_ = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Relay, 'mcp')

function Desktop-Configs {
    # The normal install, and the Microsoft Store one (which keeps its files elsewhere).
    $found = @()
    $normal = Join-Path $env:APPDATA 'Claude'
    if (Test-Path $normal) { $found += (Join-Path $normal 'claude_desktop_config.json') }
    $store = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue
    foreach ($p in $store) {
        $d = Join-Path $p.FullName 'LocalCache\Roaming\Claude'
        if (Test-Path $d) { $found += (Join-Path $d 'claude_desktop_config.json') }
    }
    return $found
}

function Update-DesktopConfig($path, [bool]$add) {
    $cfg = [pscustomobject]@{}
    if (Test-Path $path) {
        $raw = [IO.File]::ReadAllText($path)
        if ($raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
        Copy-Item $path "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"   # a backup, just in case
    }
    if (-not $cfg.PSObject.Properties['mcpServers']) { $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) }
    if ($add) {
        $entry = [pscustomobject]@{ command = 'powershell.exe'; args = $Args_ }
        $cfg.mcpServers | Add-Member -NotePropertyName uvmc -NotePropertyValue $entry -Force
    } elseif ($cfg.mcpServers.PSObject.Properties['uvmc']) {
        $cfg.mcpServers.PSObject.Properties.Remove('uvmc')
    }
    $json = $cfg | ConvertTo-Json -Depth 32
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding $false))
}

$did = $false

# Claude Code
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & $claude.Source mcp remove --scope user uvmc *> $null
    if (-not $Remove) {
        & $claude.Source mcp add --scope user uvmc -- powershell.exe @Args_
        if ($LASTEXITCODE -eq 0) { Write-Host 'Claude Code: UVMC added (for all your projects).' -ForegroundColor Green; $did = $true }
        else { Write-Host 'Claude Code: adding UVMC failed (see above).' -ForegroundColor Red }
    } else { Write-Host 'Claude Code: UVMC removed.'; $did = $true }
    $ErrorActionPreference = $old
} else {
    Write-Host 'Claude Code: not found on this PC (skipped).'
}

# Claude Desktop: UVMC comes as a desktop extension (extension\uvmc.mcpb), which Desktop
# installs itself. Older set-ups wrote UVMC into Desktop's config file; that entry is taken out
# (with a backup), since current Desktop versions don't start servers from it.
$Mcpb = Join-Path (Split-Path -Parent $PSScriptRoot) 'extension\uvmc.mcpb'
$configs = @(Desktop-Configs)
foreach ($c in $configs) {
    try {
        if ((Test-Path $c) -and ((Get-Content $c -Raw) -match '"uvmc"')) {
            Update-DesktopConfig $c $false
            Write-Host "Claude Desktop: took the old UVMC entry out of $c (backup kept next to it)."
        }
    } catch { Write-Host "Claude Desktop: could not read $c : $($_.Exception.Message)" -ForegroundColor Yellow }
}
if ($Remove) {
    Write-Host 'Claude Desktop: to remove the UVMC extension, open Claude Desktop, Settings, Extensions, and uninstall UVMC there.'
    $did = $true
} elseif (-not (Test-Path $Mcpb)) {
    Write-Host "Claude Desktop: $Mcpb is missing (skipped)." -ForegroundColor Red
} else {
    $go = $true
    if (-not $Yes) { $go = -not ((Read-Host 'Claude Desktop: install the UVMC extension? Claude Desktop opens an install window. [Y/n]') -match '^[nN]') }
    if ($go) {
        try {
            Start-Process -FilePath $Mcpb
            Write-Host 'Claude Desktop: an install window for UVMC should open in Claude Desktop. Click Install there.' -ForegroundColor Green
            Write-Host "If nothing opens, drag this file into the Claude Desktop window: $Mcpb"
            $did = $true
        } catch {
            Write-Host "Claude Desktop: could not open the extension. Drag this file into the Claude Desktop window: $Mcpb" -ForegroundColor Yellow
        }
    }
}

if ($did -and -not $Remove) {
    Write-Host ''
    Write-Host 'Last step: allow it in UVMC. Open the panel, Settings, "Claude apps on your PC", and switch it on.'
    Write-Host 'Then ask your Claude app something like: "use uvmc to make a csv of ... in the machine".'
}
if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
