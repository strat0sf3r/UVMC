# Creates a small test folder inside this repo (testdata\, ignored by git) to try shared
# folders with, and lists it in shares.txt. It never touches anything outside the repo,
# and leaves a testdata folder that already has files alone (an empty one gets filled). Start it with make-testdata.cmd.

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Dir  = Join-Path $Repo 'testdata'

if ((Test-Path $Dir) -and (Get-ChildItem -Force $Dir | Select-Object -First 1)) {
    Write-Host "testdata already has files, leaving it as it is: $Dir"
} else {
    foreach ($sub in 'models', 'textures', 'notes', 'config') {
        New-Item -ItemType Directory -Force -Path (Join-Path $Dir $sub) | Out-Null
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllText((Join-Path $Dir 'notes\readme.md'),
        "# Test project`n`nSample files for trying UVMC's shared folders.`n`n- models: two binary 'statues' (1 MB and 60 MB)`n- textures: a palette as CSV`n- config: settings as JSON`n", $utf8)
    [IO.File]::WriteAllText((Join-Path $Dir 'notes\todo.txt'),
        "fix the palette names`nraise max_statues to 12`nadd a 'marble' texture`n", $utf8)
    [IO.File]::WriteAllText((Join-Path $Dir 'config\settings.json'),
        "{`n  `"title`": `"Test project`",`n  `"max_statues`": 8,`n  `"quality`": `"medium`"`n}`n", $utf8)
    $rows = @('name,red,green,blue', 'stone,128,128,120', 'sand,194,178,128', 'moss,92,110,60', 'brick,150,70,55')
    [IO.File]::WriteAllText((Join-Path $Dir 'textures\palette.csv'), ($rows -join "`n") + "`n", $utf8)
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    foreach ($f in @(@{ Name = 'statue_small.bin'; MB = 1 }, @{ Name = 'statue_big.bin'; MB = 60 })) {
        $bytes = New-Object byte[] ($f.MB * 1MB)
        $rng.GetBytes($bytes)
        [IO.File]::WriteAllBytes((Join-Path $Dir "models\$($f.Name)"), $bytes)
    }
    Write-Host "Made test data in $Dir"
}

$List = Join-Path $Repo 'shares.txt'
if (-not (Test-Path $List)) { Copy-Item (Join-Path $Repo 'shares.example.txt') $List -ErrorAction SilentlyContinue }
$active = (Test-Path $List) -and (Select-String -Path $List -Pattern '^\s*testdata\s*=' -Quiet)
if (-not $active) {
    Add-Content -Path $List -Value 'testdata = testdata'
    Write-Host 'Added testdata to shares.txt'
}
Write-Host 'Next: run share.cmd to share it with the agent.'
Read-Host 'Press Enter to close' | Out-Null
